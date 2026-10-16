"""Bounded MCP stdio transport for one supervisor-registered managed run."""

import json
import math
import os
from pathlib import Path
import queue
import re
import select
import signal
import sys
import threading
import time

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harnessfleet.contracts import FleetError, error_record
from harnessfleet.delegation import Client, MAX_REQUEST, MAX_RESPONSE, MUTATIONS, OPERATIONS, REQUEST_ID

VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
DEADLINE = 5.0
OPERATION_NAMES = sorted(
    (
        OPERATIONS
        & {
            "routes",
            "start",
            "start_many",
            "status",
            "list",
            "events",
            "output",
            "cancel",
            "stop",
            "resume",
        }
    )
    | {"follow"}
)
UNSAFE = (
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
TOOL = {
    "name": "run",
    "description": (
        "Manage child runs of the registered managed run execution parent. "
        "The requesting actor is stdio_bridge; the exact subagent is unknown. "
        "Mutations require a caller-supplied request_id; reuse it to reconcile uncertain results."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation", "arguments"],
        "properties": {
            "operation": {"type": "string", "enum": OPERATION_NAMES},
            "arguments": {"type": "object"},
            "request_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
        },
    },
}


def _context():
    if any(key in os.environ for key in UNSAFE):
        raise ValueError()
    for key, pattern in (
        ("HARNESSFLEET_PARENT_RUN", r"[a-f0-9]{24}"),
        ("HARNESSFLEET_PARENT_GENERATION", r"[1-9][0-9]*"),
        ("HARNESSFLEET_PARENT_TOKEN", r"[a-f0-9]{64}"),
        ("HARNESSFLEET_BRIDGE_INSTANCE", r"[a-f0-9]{32}"),
    ):
        if not re.fullmatch(pattern, os.environ.get(key, "")):
            raise ValueError()
    broker = os.environ.get("HARNESSFLEET_BROKER", "")
    if not broker.startswith("/") or "\0" in broker or not os.environ.get("HARNESSFLEET_EXECUTION"):
        raise ValueError()
    owner = json.loads(os.environ.get("HARNESSFLEET_BROKER_OWNER", "null"), object_pairs_hook=_object)
    if (
        not isinstance(owner, dict)
        or set(owner) != {"pid", "birth"}
        or type(owner["pid"]) is not int
        or owner["pid"] <= 0
        or not isinstance(owner["birth"], str)
        or not owner["birth"]
    ):
        raise ValueError()


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _private(value):
    if isinstance(value, dict):
        return any(key.startswith("_") or _private(item) for key, item in value.items())
    return isinstance(value, list) and any(_private(item) for item in value)


class _Server:
    def __init__(self, cwd):
        self.cwd = cwd
        self.retired = threading.Event()
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.calls = {}
        self.slots = threading.BoundedSemaphore(2)
        self.follower = threading.BoundedSemaphore(1)
        self.jobs = queue.Queue(maxsize=2)
        self.initialized = False
        self.ready = False
        self.shutdown_requested = False
        self.wakeup_read, self.wakeup_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.out = sys.stdout.fileno()
        os.set_blocking(self.out, False)
        for _ in range(2):
            threading.Thread(target=self.worker, daemon=True).start()

    def request_shutdown(self):
        self.shutdown_requested = True
        try:
            os.write(self.wakeup_write, b"x")
        except OSError:
            pass

    def write(self, packet):
        payload = (_json(packet) + "\n").encode()
        if len(payload) > MAX_RESPONSE:
            raise FleetError("output", "MCP response exceeds its limit")
        deadline = time.monotonic() + DEADLINE
        if not self.write_lock.acquire(timeout=DEADLINE):
            self.retired.set()
            return
        try:
            view = memoryview(payload)
            while view and not self.retired.is_set() and not self.shutdown_requested:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OSError()
                _, writable, _ = select.select([self.wakeup_read], [self.out], [], remaining)
                if self.shutdown_requested:
                    return
                if not writable:
                    raise OSError()
                try:
                    view = view[os.write(self.out, view) :]
                except BlockingIOError:
                    continue
        except OSError:
            self.retired.set()
        finally:
            self.write_lock.release()

    def rpc_error(self, rid, code, message):
        self.write({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    def tool_result(self, rid, result=None, error=None, request_id=None):
        envelope = error if error is not None else {"schema_version": 1, "result": result}
        if error is not None and request_id is not None:
            envelope = {**envelope, "request_id": request_id}
        self.write(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "content": [{"type": "text", "text": _json(envelope)}],
                    "isError": error is not None,
                },
            }
        )

    def worker(self):
        while not self.retired.is_set():
            try:
                rid, operation, arguments, request_id, cancelled = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if cancelled.is_set():
                    raise FleetError("cancelled", "MCP call cancelled; admitted work may continue")
                client = Client(cancelled=cancelled)
                kwargs = dict(arguments)
                if request_id is not None:
                    kwargs["request_id"] = request_id
                result = getattr(client, "wait" if operation == "follow" else operation)(**kwargs)
                if cancelled.is_set():
                    raise FleetError("cancelled", "MCP call cancelled; admitted work may continue")
                self.tool_result(rid, result=result)
            except Exception as error:
                if isinstance(error, FleetError):
                    record = error_record(error.code, str(error))
                    # Backend messages are bounded before nesting and MCP escaping.
                    if len(_json(record)) > 65536:
                        record = error_record("output", "Backend error exceeds its limit")
                else:
                    record = error_record("internal", "Managed stdio request failed")
                try:
                    self.tool_result(rid, error=record, request_id=request_id)
                except Exception:
                    self.retired.set()
            finally:
                with self.lock:
                    self.calls.pop(rid, None)
                if operation == "follow":
                    self.follower.release()
                self.slots.release()

    def dispatch(self, packet):
        if not isinstance(packet, dict):
            self.rpc_error(None, -32600, "Invalid request")
            return
        rid = packet.get("id")
        has_id = "id" in packet
        if (
            set(packet) - {"jsonrpc", "id", "method", "params"}
            or packet.get("jsonrpc") != "2.0"
            or not isinstance(packet.get("method"), str)
            or (has_id and (type(rid) not in (str, int) or len(str(rid)) > 256))
        ):
            self.rpc_error(None, -32600, "Invalid request")
            return
        with self.lock:
            duplicate = has_id and rid in self.calls
        if duplicate:
            self.rpc_error(rid, -32600, "Request id is already active")
            return
        method, params = packet["method"], packet.get("params", {})
        if not isinstance(params, dict):
            self.rpc_error(rid, -32602, "Invalid params")
            return
        if "_meta" in params:
            if not isinstance(params["_meta"], dict):
                self.rpc_error(rid, -32602, "Invalid request metadata")
                return
            params = {key: value for key, value in params.items() if key != "_meta"}
        if method == "notifications/cancelled" and not has_id:
            if set(params) <= {"requestId", "reason"} and type(params.get("requestId")) in (str, int):
                with self.lock:
                    event = self.calls.get(params["requestId"])
                    if event is not None:
                        event.set()
            return
        if method == "notifications/initialized" and not has_id and not params and self.initialized:
            self.ready = True
            return
        if not has_id:
            return
        if method == "initialize":
            if (
                self.initialized
                or set(params) != {"protocolVersion", "capabilities", "clientInfo"}
                or not isinstance(params["protocolVersion"], str)
                or not isinstance(params["capabilities"], dict)
                or not isinstance(params["clientInfo"], dict)
            ):
                self.rpc_error(rid, -32602, "Invalid initialization")
                return
            self.initialized = True
            version = params["protocolVersion"]
            self.write(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": version if version in VERSIONS else VERSIONS[-1],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "harnessfleet-managed-stdio", "version": "1"},
                    },
                }
            )
        elif method == "ping" and not params:
            self.write({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif not self.ready:
            self.rpc_error(rid, -32600, "MCP initialization required")
        elif method == "tools/list" and set(params) <= {"cursor"} and params.get("cursor") is None:
            self.write({"jsonrpc": "2.0", "id": rid, "result": {"tools": [TOOL]}})
        elif method == "tools/call":
            self.admit(rid, params)
        else:
            self.rpc_error(rid, -32601, "Unknown method or fields")

    def admit(self, rid, params):
        request_id = None
        try:
            if set(params) != {"name", "arguments"} or params["name"] != "run":
                raise ValueError()
            tool = params["arguments"]
            if not isinstance(tool, dict) or set(tool) - {"operation", "arguments", "request_id"}:
                raise ValueError()
            operation, arguments = tool.get("operation"), tool.get("arguments")
            request_id = tool.get("request_id")
            if (
                not isinstance(operation, str)
                or operation not in OPERATION_NAMES
                or not isinstance(arguments, dict)
                or _private(arguments)
                or "request_id" in arguments
                or (
                    "request_id" in tool
                    and (not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id))
                )
                or (operation in MUTATIONS and request_id is None)
            ):
                raise ValueError()
            arguments = dict(arguments)
            if operation == "routes" and arguments.get("diagnose"):
                raise ValueError()
            if operation in {"start", "start_many"}:
                cwd = arguments.get("cwd")
                if cwd is not None and not isinstance(cwd, str):
                    raise ValueError()
                arguments["cwd"] = str((Path(self.cwd) / (cwd or ".")).resolve())
            if operation == "start_many":
                targets = arguments.get("targets")
                if not isinstance(targets, list) or not 1 <= len(targets) <= 128:
                    raise ValueError()
            if operation == "follow":
                timeout = arguments.setdefault("timeout", 120)
                if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 120:
                    raise ValueError()
        except (ValueError, TypeError, RecursionError):
            self.tool_result(rid, error=error_record("request", "Invalid run arguments"))
            return
        if self.retired.is_set() or self.shutdown_requested:
            return
        if not self.slots.acquire(blocking=False):
            self.tool_result(
                rid, error=error_record("busy", "Two tool calls are already active"), request_id=request_id
            )
            return
        if operation == "follow" and not self.follower.acquire(blocking=False):
            self.slots.release()
            self.tool_result(rid, error=error_record("busy", "A follow call is already active"))
            return
        cancelled = threading.Event()
        with self.lock:
            if self.retired.is_set() or self.shutdown_requested:
                if operation == "follow":
                    self.follower.release()
                self.slots.release()
                return
            self.calls[rid] = cancelled
        self.jobs.put_nowait((rid, operation, arguments, request_id, cancelled))

    def run(self):
        fd = sys.stdin.fileno()
        os.set_blocking(fd, False)
        buffer = bytearray()
        partial_since = None
        eof = False
        try:
            while not self.retired.is_set():
                if self.shutdown_requested:
                    eof = True
                    break
                if partial_since is not None and time.monotonic() - partial_since >= DEADLINE:
                    break
                ready = select.select([fd, self.wakeup_read], [], [], 0.1)[0]
                if self.shutdown_requested:
                    eof = True
                    break
                if fd not in ready:
                    continue
                chunk = os.read(fd, min(65536, MAX_REQUEST + 1 - len(buffer)))
                if not chunk:
                    eof = True
                    break
                if not buffer:
                    partial_since = time.monotonic()
                buffer.extend(chunk)
                while b"\n" in buffer and not self.shutdown_requested:
                    line, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    if len(line) + 1 > MAX_REQUEST:
                        return 2
                    try:
                        packet = json.loads(
                            line,
                            object_pairs_hook=_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                        )
                    except (ValueError, UnicodeError, RecursionError):
                        self.rpc_error(None, -32700, "Invalid JSON")
                    else:
                        self.dispatch(packet)
                    partial_since = time.monotonic() if buffer else None
                    if self.retired.is_set():
                        break
                if len(buffer) >= MAX_REQUEST:
                    break
        finally:
            self.retired.set()
            with self.lock:
                for event in self.calls.values():
                    event.set()
        return 0 if self.shutdown_requested or (eof and not buffer) else 2


def _retire():
    replies = queue.Queue(maxsize=1)
    cancelled = threading.Event()

    def request():
        try:
            replies.put(Client(cancelled=cancelled).bridge_retire())
        except Exception:
            replies.put(None)

    threading.Thread(target=request, daemon=True).start()
    try:
        reply = replies.get(timeout=DEADLINE)
        return isinstance(reply, dict) and reply.get("completed") is True
    except queue.Empty:
        return False
    finally:
        cancelled.set()


class _Shutdown:
    requested = False

    def is_set(self):
        return self.requested


def serve():
    """Serve MCP on stdin/stdout; invalid managed startup never opens local state."""
    shutdown = _Shutdown()
    server = None
    registration_attempted = False

    def request_shutdown(*_):
        shutdown.requested = True
        if server is not None:
            server.request_shutdown()

    previous = signal.signal(signal.SIGTERM, request_shutdown)
    previous_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    try:
        try:
            _context()
            if shutdown.is_set():
                raise ValueError()
            registration_attempted = True
            registration = Client(cancelled=shutdown).bridge_register(
                instance=os.environ["HARNESSFLEET_BRIDGE_INSTANCE"]
            )
            if shutdown.is_set():
                raise ValueError()
            if (
                not isinstance(registration, dict)
                or registration.get("schema_version") != 1
                or registration.get("bridge_instance") != os.environ["HARNESSFLEET_BRIDGE_INSTANCE"]
                or registration.get("run") != os.environ["HARNESSFLEET_PARENT_RUN"]
                or type(registration.get("generation")) is not int
                or registration["generation"] != int(os.environ["HARNESSFLEET_PARENT_GENERATION"])
                or not isinstance(registration.get("cwd"), str)
                or not registration["cwd"].startswith("/")
            ):
                raise ValueError()
        except Exception:
            if registration_attempted and shutdown.is_set():
                _retire()
            os.write(2, b"Managed stdio startup rejected\n")
            return 2
        try:
            server = _Server(registration["cwd"])
            if shutdown.is_set():
                server.request_shutdown()
            result = server.run()
            if result:
                return result
            completed = _retire()
            return 0 if completed and (server.ready or not shutdown.is_set()) else 2
        except Exception:
            os.write(2, b"Managed stdio transport retired\n")
            return 2
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        signal.signal(signal.SIGTERM, previous)
        if server is not None:
            os.close(server.wakeup_read)
            os.close(server.wakeup_write)


if __name__ == "__main__":
    if len(sys.argv) != 1:
        os.write(2, b"Managed stdio startup rejected\n")
        raise SystemExit(2)
    raise SystemExit(serve())
