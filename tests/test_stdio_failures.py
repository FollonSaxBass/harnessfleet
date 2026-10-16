"""Native stream ordering and real broker cancellation boundaries."""

import copy
import json
import os
import queue
import select
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from harnessfleet import FleetError, RunManager, processes
from harnessfleet.adapters import Codex
from harnessfleet.contracts import Result
from harnessfleet.delegation import Client, Portal
from harnessfleet import delegation
from harnessfleet.worker import native_lines


@pytest.mark.parametrize("native_exited", [False, True])
@pytest.mark.parametrize("delayed_notification", [False, True])
def test_helper_failure_precedes_buffered_native_final_output(native_exited, delayed_notification):
    read, write = os.pipe()
    helper = None
    portal = SimpleNamespace(failure_fd=read, bridge_handle=None)
    if delayed_notification:
        helper = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
        portal.bridge_handle = processes.pidfd_open(helper.pid)
    code = (
        "import sys,json;print(json.dumps({'type':'thread.started','thread_id':'synthetic'}),flush=True);"
        "sys.stdin.buffer.read(1);"
        "print(json.dumps({'type':'turn.completed'}),flush=True);"
        + ("" if native_exited else "sys.stdin.buffer.read(1)")
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        result = Result()
        lines = native_lines(process, [], portal, lambda: result.completed)
        Codex().consume(json.loads(next(lines)), result)
        if helper is not None:
            helper.kill()
            helper.wait()
        else:
            os.write(write, b"helper failed first")
        process.stdin.write(b"x")
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 3)[0]
        if native_exited:
            process.wait(timeout=3)
        with pytest.raises(FleetError) as failure:
            for line in lines:
                Codex().consume(json.loads(line), result)
                if result.completed and not native_exited:
                    process.stdin.write(b"x")
                    process.stdin.flush()
        assert failure.value.code == "delegation_transport"
        assert not result.completed
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        os.close(read)
        os.close(write)
        if helper is not None:
            if helper.poll() is None:
                helper.kill()
            helper.wait()
            os.close(portal.bridge_handle)


def test_cancelled_followers_release_real_broker_capacity_without_stopping_child(instance, monkeypatch):
    parent = instance.start("mode:slow")
    watch = processes.Changes(instance.m.store.root)
    portal = None
    try:
        deadline = time.monotonic() + 5
        while not instance.m.store.get(parent["id"]).get("native_process"):
            assert time.monotonic() < deadline
            watch.wait(1)
        stored = instance.m.store.get(parent["id"])
        child_manager = copy.copy(instance.m)
        child_manager._principal = {"id": parent["id"], "generation": 1, "token": stored["child_token"]}
        child = child_manager.start("claude-local", "mode:slow")
        # This synthetic native root is this test process; actual socket credentials
        # and the real broker/Store/follower implementation remain in use.
        with instance.m.store.transaction() as c:
            current = instance.m.store.get(parent["id"], c)
            current["native_process"] = processes.receipt(os.getpid())
            instance.m.store.save(c, current, "synthetic_native_root")
        portal = Portal(instance.m, parent["id"], 1)
        portal.bind_native(processes.receipt(os.getpid()))
        # Supply the client's environment without turning the host broker into
        # a managed client in this single-process boundary fixture.
        client_os = SimpleNamespace(**vars(os))
        client_os.environ = {
            **os.environ,
            **portal.environment(),
            "HARNESSFLEET_PARENT_TOKEN": stored["child_token"],
        }
        monkeypatch.setattr(delegation, "os", client_os)
        monkeypatch.setattr(delegation, "managed_environment", lambda: False)
        entered = queue.Queue()
        original = RunManager.wait

        def wait(self, rid, timeout=None):
            if self._principal:
                entered.put(True)
            return original(self, rid, timeout)

        monkeypatch.setattr(RunManager, "wait", wait)
        for number in range(10):
            cancelled = threading.Event()
            errors = []

            def follow():
                try:
                    Client(cancelled=cancelled).wait(child["id"], 120)
                except FleetError as error:
                    errors.append(error.code)

            thread = threading.Thread(target=follow, daemon=True)
            thread.start()
            try:
                entered.get(timeout=1)
            except queue.Empty:
                pytest.fail(f"Broker could not admit follower {number + 1}: {errors}")
            cancelled.set()
            thread.join(timeout=1)
            assert not thread.is_alive() and errors == ["cancelled"]
        assert Client().status(child["id"])["status"] in ("starting", "running")
    finally:
        if portal is not None:
            portal.close()
        if "stored" in locals():
            with instance.m.store.transaction() as c:
                current = instance.m.store.get(parent["id"], c)
                current["native_process"] = stored["native_process"]
                instance.m.store.save(c, current, "synthetic_native_restored")
        watch.close()


@pytest.mark.parametrize("helper_failed", [False, True])
def test_progress_error_wakes_idle_reader_and_preserves_helper_failure_precedence(helper_failed):
    from harnessfleet.progress import ProgressWriter

    class BrokenStore:
        def persist_progress_batch(self, *args, **kwargs):
            raise OSError("synthetic persistence failure")

    process = subprocess.Popen(
        [sys.executable, "-u", "-c", 'print(\'{"type":"thread.started"}\',flush=True);input()'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    read, write = os.pipe()
    portal = SimpleNamespace(failure_fd=read, bridge_handle=None)
    progress = ProgressWriter(BrokenStore(), "a" * 24, 1)
    lines = native_lines(process, [], portal, progress=progress)
    observed = queue.Queue()
    reader = None
    try:
        assert json.loads(next(lines))["type"] == "thread.started"
        if helper_failed:
            os.write(write, b"helper failed")
        progress.submit("thread.started", "synthetic-session")

        def read_next():
            try:
                next(lines)
            except Exception as error:
                observed.put(error.code if isinstance(error, FleetError) else "execution")
            else:
                observed.put("unexpected native output")

        reader = threading.Thread(target=read_next)
        reader.start()
        assert observed.get(timeout=2) == ("delegation_transport" if helper_failed else "execution")
    finally:
        progress.cancel()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        if reader is not None:
            reader.join(timeout=2)
            assert not reader.is_alive()
        lines.close()
        assert progress.join()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        os.close(read)
        os.close(write)
