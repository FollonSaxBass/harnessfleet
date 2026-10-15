import json
import socket
import struct
import time

import pytest
from harnessfleet import FleetError, processes


def test_native_delegation_cannot_observe_or_stop_an_unrelated_run(instance, monkeypatch):
    victim = instance.m.start("claude-local", "mode:slow")
    monkeypatch.setenv("HF_TEST_VICTIM", victim["id"])
    attacker = instance.start("mode:foreign-control")
    assert instance.finish(attacker)["status"] == "finished"
    outcomes = json.loads(instance.m.output(attacker["id"])["text"])
    assert outcomes == {"status": "delegation_scope", "stop": "delegation_scope"}
    assert instance.m.status(victim["id"])["status"] in ("starting", "running")


def test_required_worker_isolation_refuses_unqualified_host_before_effects(instance):
    instance.config["policy"]["worker_isolation"] = "isolated"
    instance.save()
    with pytest.raises(FleetError) as error:
        instance.start()
    assert error.value.code == "isolation"
    assert not instance.calls() and instance.m.list()["total"] == 0


def test_unavailable_transport_does_not_consume_native_invocation(instance, monkeypatch):
    temporary = instance.path / ("long-socket-directory-" * 6)
    temporary.mkdir()
    monkeypatch.setenv("TMPDIR", str(temporary))
    run = instance.start()
    result = instance.finish(run)
    assert result["status"] == "failed"
    assert not result["invoked"] and not instance.calls()
    assert result["failure"] == "delegation_transport"


@pytest.mark.parametrize("lost_reply", [False, True])
def test_worker_request_ids_preserve_one_child_without_opening_store(instance, lost_reply):
    parent = instance.start("mode:idempotent" + (" mode:lost-reply" if lost_reply else ""))
    assert instance.finish(parent)["status"] == "finished"
    result = json.loads(instance.m.output(parent["id"])["text"])
    assert result["ids"][0] == result["ids"][1]
    assert result["verified"] and result["children"] == 1
    assert result["conflict"] == "request_conflict"
    assert len(instance.calls()) == 2


@pytest.mark.parametrize(
    "task,code", [("mode:cleared-parent", "permission"), ("mode:missing-broker", "delegation_transport")]
)
def test_worker_context_cannot_fall_back_to_root_admission(instance, task, code):
    parent = instance.start(task)
    assert instance.finish(parent)["status"] == "finished"
    assert instance.m.output(parent["id"])["text"] == code
    assert instance.m.list()["total"] == 1 and len(instance.calls()) == 1


def test_copied_worker_credentials_do_not_authorize_a_host_sibling(instance, monkeypatch):
    witness = instance.path / "capability.json"
    monkeypatch.setenv("HF_TEST_CAPABILITY_WITNESS", str(witness))
    watch = processes.Changes(instance.path)
    try:
        parent = instance.start("mode:capability mode:slow")
        deadline = time.monotonic() + 5
        while not witness.exists():
            assert time.monotonic() < deadline
            watch.wait(1)
        credentials = json.loads(witness.read_text())
        with monkeypatch.context() as copied:
            for key, value in credentials.items():
                copied.setenv(key, value)
            result = instance.cli("run", "list", ok=False)
        assert result.returncode == 2
        assert json.loads(result.stderr)["error"]["code"] == "delegation_auth"
        assert instance.m.status(parent["id"])["status"] == "running"
        assert len(instance.calls()) == 1
    finally:
        watch.close()


def test_transferred_connection_checks_actual_message_writer(instance, monkeypatch):
    witness = instance.path / "capability.json"
    monkeypatch.setenv("HF_TEST_CAPABILITY_WITNESS", str(witness))
    path = str(instance.path / "handoff.sock")
    monkeypatch.setenv("HF_TEST_HANDOFF", path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as handoff:
        handoff.bind(path)
        handoff.listen()
        handoff.settimeout(5)
        instance.start("mode:capability mode:handoff mode:slow")
        connection, _ = handoff.accept()
        with connection:
            _, controls, _, _ = connection.recvmsg(1, socket.CMSG_SPACE(4))
        level, kind, value = controls[0]
        assert level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS
        fd = struct.unpack("i", value)[0]
        credentials = json.loads(witness.read_text())
        with socket.socket(fileno=fd) as transferred:
            transferred.settimeout(5)
            packet = {
                "schema_version": 1,
                "operation": "routes",
                "args": [],
                "kwargs": {},
                "token": credentials["HARNESSFLEET_PARENT_TOKEN"],
            }
            transferred.sendall((json.dumps(packet) + "\n").encode())
            with transferred.makefile("rb") as stream:
                response = json.loads(stream.readline(100000))
        assert response["error"]["code"] == "delegation_auth"
        assert len(instance.calls()) == 1
