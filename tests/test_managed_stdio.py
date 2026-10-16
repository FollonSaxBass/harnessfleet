"""Exercise the wire protocol in isolated processes with a synthetic broker client."""

import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import pytest
from harnessfleet import managed_stdio

SOURCE = Path(managed_stdio.__file__).resolve().parents[1]
DRIVER = r"""
import json
import os
from pathlib import Path
import sys
import threading
import time
import signal
sys.path.insert(0, sys.argv[1])
from harnessfleet import managed_stdio as helper
from harnessfleet.contracts import FleetError

class Client:
    admitted = threading.Event()

    def __init__(self, cancelled=None):
        self.cancelled = cancelled

    def bridge_register(self, *, instance):
        os.write(2, b"fixture registration\n")
        if os.environ.get("HF_PROTO_REGISTER_WAIT"):
            if self.cancelled is None:
                time.sleep(60)
            else:
                while not self.cancelled.is_set():
                    time.sleep(0.001)
            raise FleetError("cancelled", "Registration response cancelled")
        if os.environ.get("HF_PROTO_REGISTER_RETURN_SIGNAL"):
            os.kill(os.getpid(), 15)
        return dict(schema_version=1, cwd="/recorded/parent", bridge_instance=instance,
                    run=os.environ["HARNESSFLEET_PARENT_RUN"], generation=3)

    def bridge_retire(self):
        if os.environ.get("HF_PROTO_REGISTER_WAIT"):
            os.write(2, b"fixture retirement\n")
        if os.environ.get("HF_PROTO_RETIRE_WAIT"):
            time.sleep(60)
        return {"completed": True}

    def __getattr__(self, operation):
        def call(**kwargs):
            behavior = kwargs.pop("behavior", None)
            if operation in {"start", "start_many"}:
                self.admitted.set()
            if behavior == "backpressure":
                return "x" * (1024 * 1024)
            if behavior == "wait":
                # The durable admission survives the cancellation of this socket waiter.
                while not self.cancelled.wait(0.01):
                    pass
                raise FleetError("cancelled", "Waiter cancelled; admitted child continues")
            if behavior == "ignore_cancel":
                time.sleep(60)
            if behavior == "large":
                return "\\\"" * (helper.MAX_RESPONSE // 2)
            if behavior == "error":
                raise FleetError("delegation_transport", "Reply unavailable")
            if behavior == "unexpected":
                raise RuntimeError("SECRET_SENTINEL")
            if behavior == "receipt":
                return {"admitted": self.admitted.wait(1)}
            return dict(operation=operation, arguments=kwargs)
        return call
helper.Client = Client
if os.environ.get("HF_PROTO_SERVER_INIT_SIGNAL"):
    original_init = helper._Server.__init__
    def interrupted_init(server, cwd):
        original_init(server, cwd)
        os.kill(os.getpid(), 15)
    helper._Server.__init__ = interrupted_init
if os.environ.get("HF_PROTO_BLOCK_SIGTERM"):
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
if os.environ.get("HF_PROTO_SHUTDOWN_FRAME") or os.environ.get("HF_PROTO_NOTIFY_FRAME"):
    original = helper._Server.dispatch
    def shutdown_after_frame(server, packet):
        if packet.get("id") == 100 and os.environ.get("HF_PROTO_NOTIFY_FRAME"):
            os.write(2, b"fixture dispatch100\n")
        original(server, packet)
        if packet.get("id") == 99 and os.environ.get("HF_PROTO_SHUTDOWN_FRAME"):
            os.kill(os.getpid(), 15)
    helper._Server.dispatch = shutdown_after_frame
sys.exit(helper.serve())
"""


class Wire:
    def __init__(self, process):
        self.process = process
        self.buffer = bytearray()

    def send(self, packet):
        self.raw((json.dumps(packet) + "\n").encode())

    def raw(self, data):
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def recv(self, timeout=3):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "MCP response timed out"
            assert select.select([self.process.stdout], [], [], remaining)[0], "MCP response timed out"
            chunk = os.read(self.process.stdout.fileno(), 65536)
            assert chunk, self.process.stderr.read().decode()
            self.buffer.extend(chunk)
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        return json.loads(line)

    def request(self, method, params=None, rid=1):
        self.send(dict(jsonrpc="2.0", id=rid, method=method, params=params or {}))
        return self.recv()

    def initialize(self, version="2025-11-25"):
        result = self.request(
            "initialize",
            dict(protocolVersion=version, capabilities={}, clientInfo=dict(name="fixture", version="1")),
        )
        self.send(dict(jsonrpc="2.0", method="notifications/initialized"))
        assert "result" in self.request("tools/list")
        return result

    def call(self, operation="status", arguments=None, rid=2, request_id=None):
        tool = dict(operation=operation, arguments=arguments or {})
        if request_id is not None:
            tool["request_id"] = request_id
        self.send(dict(jsonrpc="2.0", id=rid, method="tools/call", params=dict(name="run", arguments=tool)))

    def close(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=7)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise
        self.process.stdout.close()
        self.process.stderr.close()


def envelope(packet):
    return json.loads(packet["result"]["content"][0]["text"])


@pytest.fixture
def launch(tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    wires = []

    def start(changes=None, script=False, extra=()):
        env = dict(
            HARNESSFLEET_PARENT_RUN="a" * 24,
            HARNESSFLEET_PARENT_GENERATION="3",
            HARNESSFLEET_PARENT_TOKEN="b" * 64,
            HARNESSFLEET_BRIDGE_INSTANCE="c" * 32,
            HARNESSFLEET_BROKER="/unused/broker.sock",
            HARNESSFLEET_BROKER_OWNER=json.dumps(dict(pid=1, birth="fixture")),
            HARNESSFLEET_EXECUTION="native-mcp",
        )
        for key, value in (changes or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        command = [sys.executable, "-I", "-B", str(driver), str(SOURCE)]
        if script:
            command = [sys.executable, "-I", "-B", str(SOURCE / "harnessfleet" / "managed_stdio.py")]
        command.extend(extra)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=tmp_path,
            bufsize=0,
        )
        wire = Wire(process)
        wires.append(wire)
        return wire

    yield start
    for wire in wires:
        wire.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"HARNESSFLEET_PARENT_RUN": None},
        {"HARNESSFLEET_PARENT_RUN": "../bad"},
        {"HARNESSFLEET_PARENT_GENERATION": "0"},
        {"HARNESSFLEET_PARENT_TOKEN": "x" * 64},
        {"HARNESSFLEET_BRIDGE_INSTANCE": "bad"},
        {"HARNESSFLEET_BROKER": "relative"},
        {"HARNESSFLEET_EXECUTION": ""},
        {"HARNESSFLEET_BROKER_OWNER": "{}"},
        {"HARNESSFLEET_BROKER_OWNER": '{"pid":true,"birth":"x"}'},
        *[
            {key: "sentinel"}
            for key in (
                "HARNESSFLEET_CONFIG",
                "HARNESSFLEET_STATE",
                "HARNESSFLEET_SERVICE_TOKEN",
                "CODEX_HOME",
                "CODEX_SQLITE_HOME",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "PYTHONPATH",
                "PYTHONHOME",
                "HARNESSFLEET_BOX",
                "CODEX_BOX_ACCOUNT",
                "CURSOR_BOX_ACCOUNT",
            )
        ],
    ],
)
def test_invalid_context_has_no_stdout_or_files(launch, tmp_path, changes):
    before = set(tmp_path.iterdir())
    wire = launch(changes)
    assert wire.process.wait(timeout=3) == 2
    assert wire.process.stdout.read() == b""
    assert wire.process.stderr.read() == b"Managed stdio startup rejected\n"
    assert set(tmp_path.iterdir()) == before


def test_pinned_script_imports_without_cwd_or_pythonpath(launch):
    wire = launch({"HARNESSFLEET_PARENT_RUN": None}, script=True)
    assert wire.process.wait(timeout=3) == 2
    assert wire.process.stdout.read() == b""
    assert wire.process.stderr.read() == b"Managed stdio startup rejected\n"


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "future"])
def test_initialization_and_public_tool(launch, version):
    wire = launch()
    expected = "2025-11-25" if version == "future" else version
    assert wire.initialize(version)["result"]["protocolVersion"] == expected
    tools = wire.request("tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["run"]
    assert set(tools[0]["inputSchema"]["properties"]["operation"]["enum"]) == {
        "routes",
        "start",
        "start_many",
        "status",
        "list",
        "events",
        "follow",
        "output",
        "cancel",
        "stop",
        "resume",
    }
    assert "stdio_bridge" in tools[0]["description"]
    assert "unknown" in tools[0]["description"]
    assert wire.request("ping")["result"] == {}
    wire.call(arguments={"rid": "child"})
    assert envelope(wire.recv())["result"] == dict(operation="status", arguments={"rid": "child"})


@pytest.mark.parametrize("operation", ["start", "start_many", "cancel", "stop", "resume"])
def test_mutations_require_supplied_id(launch, operation):
    wire = launch()
    wire.initialize()
    wire.call(operation, {"targets": ["one"]})
    assert envelope(wire.recv())["error"]["code"] == "request"
    for _ in range(2):
        wire.call(operation, {"targets": ["one"]}, request_id="d" * 32)
        result = envelope(wire.recv())["result"]
        assert result["arguments"]["request_id"] == "d" * 32


@pytest.mark.parametrize(
    "cwd,expected",
    [(None, "/recorded/parent"), ("child", "/recorded/parent/child"), ("/absolute", "/absolute")],
)
def test_start_uses_registered_cwd(launch, cwd, expected):
    wire = launch()
    wire.initialize()
    wire.call("start", {} if cwd is None else {"cwd": cwd}, request_id="d" * 32)
    assert envelope(wire.recv())["result"]["arguments"]["cwd"] == expected


@pytest.mark.parametrize(
    "packet",
    [
        [],
        [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "extra": True},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": "unknown"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"extra": True}},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "run", "arguments": {"operation": "status", "arguments": {}}, "extra": True},
        },
    ],
)
def test_invalid_rpc(launch, packet):
    wire = launch()
    wire.initialize()
    wire.send(packet)
    response = wire.recv()
    assert "error" in response or response["result"]["isError"] is True


@pytest.mark.parametrize(
    "tool",
    [
        {"operation": "wait", "arguments": {}},
        {"operation": "status", "arguments": {}, "extra": True},
        {"operation": "status", "arguments": {"_private": True}},
        {"operation": "status", "arguments": {"nested": {"_private": True}}},
        {"operation": "status", "arguments": {"request_id": "d" * 32}},
        {"operation": "routes", "arguments": {"diagnose": True}},
        {"operation": "status", "arguments": {}, "request_id": "bad"},
        *[{"operation": "follow", "arguments": {"timeout": value}} for value in [-1, 121, None, True]],
        {"operation": "start_many", "arguments": {"targets": ["x"] * 129}, "request_id": "d" * 32},
    ],
)
def test_invalid_tool_arguments(launch, tool):
    wire = launch()
    wire.initialize()
    response = wire.request("tools/call", {"name": "run", "arguments": tool})
    assert response["result"]["isError"] is True
    assert envelope(response)["error"]["code"] == "request"


def test_malformed_and_duplicate_json_fields(launch):
    wire = launch()
    wire.raw(b"{bad}\n")
    assert wire.recv()["error"]["code"] == -32700
    wire.raw(b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n')
    assert wire.recv()["error"]["code"] == -32700


def test_two_slots_duplicate_ids_cancellation_and_surviving_work(launch):
    wire = launch()
    wire.initialize()
    wire.call("start", {"behavior": "wait"}, rid=10, request_id="d" * 32)
    wire.call("status", {"behavior": "wait"}, rid=11)
    wire.call(rid=12)
    assert envelope(wire.recv())["error"]["code"] == "busy"
    assert wire.request("ping", rid=10)["error"]["code"] == -32600
    wire.send(dict(jsonrpc="2.0", method="notifications/cancelled", params={"requestId": 10}))
    response = wire.recv()
    assert response["id"] == 10
    assert envelope(response)["request_id"] == "d" * 32
    assert envelope(response)["error"]["code"] == "cancelled"
    wire.call("status", {"behavior": "receipt"}, rid=12)
    assert envelope(wire.recv())["result"] == {"admitted": True}


def test_single_follower_and_default_timeout(launch):
    wire = launch()
    wire.initialize()
    wire.call("follow", {"behavior": "wait"}, rid=10)
    wire.call("follow", rid=11)
    assert envelope(wire.recv())["error"]["code"] == "busy"
    wire.call("status", rid=12)
    assert wire.recv()["id"] == 12
    wire.send(dict(jsonrpc="2.0", method="notifications/cancelled", params={"requestId": 10}))
    assert wire.recv()["id"] == 10
    wire.call("follow", rid=13)
    assert envelope(wire.recv())["result"] == dict(operation="wait", arguments={"timeout": 120})


@pytest.mark.parametrize(
    "behavior,code", [("error", "delegation_transport"), ("unexpected", "internal"), ("large", "output")]
)
def test_uncertain_mutation_errors_preserve_id_and_redact(launch, behavior, code):
    wire = launch()
    wire.initialize()
    wire.call("start", {"behavior": behavior}, request_id="d" * 32)
    response = wire.recv(timeout=6)
    assert envelope(response)["request_id"] == "d" * 32
    assert envelope(response)["error"]["code"] == code
    assert "SECRET_SENTINEL" not in json.dumps(response)
    assert len(json.dumps(response).encode()) < 32 * 1024 * 1024


def test_oversize_input_retires(launch):
    wire = launch()
    try:
        wire.raw(b"x" * (1024 * 1024 + 1))
    except BrokenPipeError:
        pass
    assert wire.process.wait(timeout=3) == 2


def test_partial_frame_has_deadline(launch):
    wire = launch()
    wire.raw(b'{"jsonrpc":')
    assert wire.process.wait(timeout=7) == 2


def test_eof_exits_even_if_backend_ignores_cancellation(launch):
    wire = launch()
    wire.initialize()
    wire.call(arguments={"behavior": "ignore_cancel"})
    wire.process.stdin.close()
    wire.process.wait(timeout=3)


def test_stdout_backpressure_retires(launch):
    wire = launch()
    wire.initialize()
    wire.call(arguments={"behavior": "backpressure"})
    assert wire.process.wait(timeout=8) == 2


def test_script_rejects_state_arguments(launch):
    wire = launch(script=True, extra=("--state", "/untrusted"))
    assert wire.process.wait(timeout=3) == 2
    assert wire.process.stdout.read() == b""
    assert wire.process.stderr.read() == b"Managed stdio startup rejected\n"


@pytest.mark.parametrize("timeout", [0, 120])
def test_follow_timeout_boundaries(launch, timeout):
    wire = launch()
    wire.initialize()
    wire.call("follow", {"timeout": timeout})
    assert envelope(wire.recv())["result"]["arguments"]["timeout"] == timeout


def test_batch_limit_and_selection_forwarding(launch):
    wire = launch()
    wire.initialize()
    target = {"route": "synthetic", "model": "synthetic-model"}
    selection = {"allowed": [target], "required": [target]}
    wire.call("start_many", {"targets": [target] * 128, "selection": selection}, request_id="d" * 32)
    result = envelope(wire.recv())["result"]["arguments"]
    assert len(result["targets"]) == 128
    assert result["selection"] == selection
    assert result["cwd"] == "/recorded/parent"


def test_native_request_metadata_is_not_execution_arguments(launch):
    wire = launch()
    wire.initialize()
    reply = wire.request("tools/list", {"_meta": {"progressToken": "synthetic"}, "cursor": None})
    assert [tool["name"] for tool in reply["result"]["tools"]] == ["run"]
    reply = wire.request(
        "tools/call",
        {
            "name": "run",
            "arguments": {"operation": "status", "arguments": {}},
            "_meta": {"progressToken": "synthetic"},
        },
    )
    assert envelope(reply)["result"] == {"operation": "status", "arguments": {}}
    assert wire.request("tools/list", {"_meta": "invalid"})["error"]["code"] == -32602


def test_eof_retirement_has_a_bounded_deadline(launch):
    wire = launch({"HF_PROTO_RETIRE_WAIT": "1"})
    wire.initialize()
    wire.process.stdin.close()
    assert wire.process.wait(timeout=7) == 2


@pytest.mark.parametrize("blocked", [False, True])
def test_native_sigterm_uses_bounded_graceful_retirement(launch, blocked):
    wire = launch({"HF_PROTO_RETIRE_WAIT": "1"} if blocked else None)
    wire.initialize()
    wire.process.send_signal(signal.SIGTERM)
    assert wire.process.wait(timeout=7) == (2 if blocked else 0)


def test_sigterm_discards_unfinished_frame_without_dispatch(launch):
    wire = launch()
    wire.initialize()
    wire.raw(b'{"jsonrpc":"2.0","id":99,"method":"ping"}\n{"jsonrpc":')
    assert wire.recv()["id"] == 99
    wire.process.send_signal(signal.SIGTERM)
    assert wire.process.wait(timeout=7) == 0


def test_sigterm_stops_dispatch_of_remaining_buffered_frames(launch):
    wire = launch({"HF_PROTO_SHUTDOWN_FRAME": "1"})
    wire.initialize()
    wire.raw(b'{"jsonrpc":"2.0","id":99,"method":"ping"}\n{"jsonrpc":"2.0","id":100,"method":"ping"}\n')
    assert wire.recv()["id"] == 99
    assert wire.process.wait(timeout=3) == 0
    assert wire.buffer + wire.process.stdout.read() == b""


def test_sigterm_does_not_wait_for_stdout_backpressure(launch):
    wire = launch()
    wire.initialize()
    wire.call(arguments={"behavior": "backpressure"})
    assert select.select([wire.process.stdout], [], [], 3)[0]
    wire.process.send_signal(signal.SIGTERM)
    assert wire.process.wait(timeout=1) == 0


def test_sigterm_interrupts_main_thread_stdout_backpressure(launch):
    wire = launch({"HF_PROTO_NOTIFY_FRAME": "1"})
    wire.initialize()
    assert wire.process.stderr.readline() == b"fixture registration\n"
    fcntl.fcntl(wire.process.stdout, fcntl.F_SETPIPE_SZ, 4096)
    wire.raw(b'{"jsonrpc":"2.0","id":99,"method":"ping"}\n{"jsonrpc":"2.0","id":100,"method":"ping"}\n')
    assert select.select([wire.process.stderr], [], [], 3)[0]
    assert wire.process.stderr.readline() == b"fixture dispatch100\n"
    wire.process.send_signal(signal.SIGTERM)
    assert wire.process.wait(timeout=1) == 0


def test_sigterm_is_not_disabled_by_an_inherited_mask(launch):
    wire = launch({"HF_PROTO_BLOCK_SIGTERM": "1"})
    wire.initialize()
    wire.process.send_signal(signal.SIGTERM)
    assert wire.process.wait(timeout=1) == 0


def test_sigterm_during_registration_retires_without_default_signal_exit(launch):
    wire = launch({"HF_PROTO_REGISTER_WAIT": "1"})
    assert select.select([wire.process.stderr], [], [], 3)[0]
    assert wire.process.stderr.readline() == b"fixture registration\n"
    wire.process.send_signal(signal.SIGTERM)
    assert wire.process.wait(timeout=1) == 2
    assert b"fixture retirement\n" in wire.process.stderr.read()


@pytest.mark.parametrize("phase", ["REGISTER_RETURN", "SERVER_INIT"])
def test_cancelled_startup_never_succeeds_after_valid_registration(launch, phase):
    wire = launch({"HF_PROTO_" + phase + "_SIGNAL": "1"})
    assert wire.process.wait(timeout=1) == 2
    assert wire.process.stdout.read() == b""
