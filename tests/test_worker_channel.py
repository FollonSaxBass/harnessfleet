"""Behavior of the internal, disconnected receive-only worker channel."""

import errno
import os
from contextlib import contextmanager

import pytest

from harnessfleet import FleetError
from harnessfleet.boundary import LaunchSpec, launch
from harnessfleet.runtime import RuntimeManifest
from test_boundary import kernel_boundary as kernel_boundary


@contextmanager
def channel_writer(runtime, action):
    handle = launch(
        LaunchSpec(
            "import os,socket,struct,signal; "
            "s=socket.socket(fileno=int(os.environ['HF_CHANNEL_FD'])); "
            "frame=lambda n,b:struct.pack('!4sQII',b'HFCP',1,n,len(b))+b; "
            + action
            + ";print('ready',flush=True);signal.pause()",
            runtime=runtime,
        ),
        control_channel=True,
    )
    try:
        assert handle.process.stdout.readline() == b"ready\n"
        yield handle
    finally:
        assert handle.terminate(3).cleanup_confirmed
        handle.close()


@pytest.mark.parametrize("value", [None, 0, 1, "yes"])
def test_channel_opt_in_requires_boolean_before_process_launch(monkeypatch, value):
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("native effect"))
    with pytest.raises(FleetError):
        launch(LaunchSpec("pass", runtime=RuntimeManifest(())), control_channel=value)


def test_channel_reserved_environment_is_rejected_before_process_launch(monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("native effect"))
    with pytest.raises(FleetError):
        launch(
            LaunchSpec("pass", runtime=RuntimeManifest(()), environment={"HF_CHANNEL_FD": "123"}),
            control_channel=True,
        )


def test_channel_is_handle_owned_and_receive_only(kernel_boundary):
    with channel_writer(kernel_boundary, "s.send(frame(0,b'ok'))") as handle:
        receiver = handle.control_channel
        with pytest.raises(AttributeError):
            handle.control_channel = None
        assert all(not hasattr(receiver, name) for name in ("send", "sendmsg", "fileno", "bind"))
        assert receiver.receive(timeout=3) == b"ok"


@pytest.mark.parametrize("timeout", [None, False, 0, -1, float("inf"), float("nan")])
def test_invalid_timeout_does_not_consume_record(kernel_boundary, timeout):
    with channel_writer(kernel_boundary, "s.send(frame(0,b'ok'))") as handle:
        with pytest.raises(FleetError, match="timeout"):
            handle.control_channel.receive(timeout=timeout)
        assert handle.control_channel.receive(timeout=3) == b"ok"


@pytest.mark.parametrize(
    "action",
    [
        "s.send(b'bad-frame')",
        "s.send(frame(1,b'wrong-sequence'))",
        "s.send(struct.pack('!4sQII',b'HFCP',2,0,1)+b'x')",
        "s.send(b'x'*4097)",
        "import array; r,w=os.pipe(); s.sendmsg([frame(0,b'rights')],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[r,w]))]);os.close(r);os.close(w)",
        "import array; r,w=os.pipe(); s.sendmsg([frame(0,b'rights')],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[r]*32))]);os.close(r);os.close(w)",
    ],
)
def test_rejected_record_closes_rights_and_keeps_expected_sequence(kernel_boundary, action):
    with channel_writer(kernel_boundary, action + ";s.send(frame(0,b'ok'))") as handle:
        before = set(os.listdir("/proc/self/fd"))
        with pytest.raises(FleetError):
            handle.control_channel.receive(timeout=3)
        assert set(os.listdir("/proc/self/fd")) == before
        assert handle.control_channel.receive(timeout=3) == b"ok"


def test_fork_writer_cannot_impersonate_original(kernel_boundary):
    action = (
        "child=os.fork(); "
        "s.send(frame(0,b'child')) if child==0 else None; "
        "os._exit(0) if child==0 else os.waitpid(child,0)"
    )
    with channel_writer(kernel_boundary, action + ";s.send(frame(0,b'parent'))") as handle:
        with pytest.raises(FleetError, match="writer refused"):
            handle.control_channel.receive(timeout=3)
        assert handle.control_channel.receive(timeout=3) == b"parent"


def test_invalid_records_exhaust_fixed_receive_budget(kernel_boundary):
    with channel_writer(
        kernel_boundary, "[s.send(b'bad') for _ in range(16)];s.send(frame(0,b'late'))"
    ) as handle:
        for _ in range(16):
            with pytest.raises(FleetError, match="frame refused"):
                handle.control_channel.receive(timeout=3)
        with pytest.raises(FleetError, match="budget exhausted"):
            handle.control_channel.receive(timeout=3)


def test_uncertain_close_revokes_before_refusing_live_cleanup(kernel_boundary):
    with channel_writer(kernel_boundary, "s.send(frame(0,b'queued'))") as handle:
        with pytest.raises(FleetError, match="live|confirmed|uncertain"):
            handle.close()
        with pytest.raises(FleetError, match="revoked"):
            handle.control_channel.receive(timeout=3)


def test_dead_original_writer_cannot_supply_queued_output(kernel_boundary):
    handle = launch(
        LaunchSpec(
            "import os,socket,struct; s=socket.socket(fileno=int(os.environ['HF_CHANNEL_FD']));"
            "s.send(struct.pack('!4sQII',b'HFCP',1,0,6)+b'queued')",
            runtime=kernel_boundary,
        ),
        control_channel=True,
    )
    try:
        assert handle.wait(3).cleanup_confirmed
        with pytest.raises(FleetError, match="inactive"):
            handle.control_channel.receive(timeout=3)
    finally:
        assert handle.terminate(3).cleanup_confirmed
        handle.close()


def test_original_writer_can_send_bounded_frame(kernel_boundary):
    handle = launch(
        LaunchSpec(
            "import os,socket,struct,signal; "
            "s=socket.socket(fileno=int(os.environ['HF_CHANNEL_FD'])); "
            "s.send(struct.pack('!4sQII',b'HFCP',1,0,5)+b'hello');signal.pause()",
            runtime=kernel_boundary,
        ),
        control_channel=True,
    )
    try:
        assert handle.control_channel.receive(timeout=3) == b"hello"
    finally:
        assert handle.terminate(3).cleanup_confirmed
        handle.close()


@pytest.mark.parametrize("secondary", [False, True])
@pytest.mark.parametrize("kind", [RuntimeError, KeyboardInterrupt])
def test_post_spawn_failure_retains_same_handle_and_primary(kernel_boundary, monkeypatch, secondary, kind):
    from harnessfleet import worker_channel
    from harnessfleet.runtime import PreparedRuntime

    primary = kind("controlled pre-grant refusal")
    retained = []
    original_close = PreparedRuntime.close

    def refuse(receiver, handle):
        retained.append(handle)
        raise primary

    def close_then_refuse(prepared):
        original_close(prepared)
        raise OSError(errno.ENOSPC, "controlled secondary cleanup failure")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(worker_channel._Receiver, "_bind", refuse)
            if secondary:
                fault.setattr(PreparedRuntime, "close", close_then_refuse)
            with pytest.raises(BaseException) as caught:
                launch(
                    LaunchSpec("print('forbidden-effect',flush=True)", runtime=kernel_boundary),
                    control_channel=True,
                )
        assert caught.value is primary
        assert caught.value.handle is retained[0]
        if secondary:
            assert caught.value.__notes__
        assert retained[0].wait(3).cleanup_confirmed
        assert b"forbidden-effect" not in retained[0].process.stdout.read()
        with pytest.raises(FleetError, match="revoked"):
            retained[0].control_channel.receive(timeout=1)
    finally:
        for handle in retained:
            assert handle.terminate(3).cleanup_confirmed
            handle.close()


def test_close_attempts_owned_cleanup_after_channel_close_failure(kernel_boundary, monkeypatch):
    from harnessfleet import worker_channel

    handle = launch(LaunchSpec("pass", runtime=kernel_boundary), control_channel=True)
    assert handle.wait(3).cleanup_confirmed
    original = worker_channel._Receiver.revoke
    failure = PermissionError("controlled secondary endpoint failure")
    owned_fds = [handle.launcher_fd, handle.init_fd, handle.native_fd, handle.namespace_fd]

    def close_then_fail(receiver):
        original(receiver)
        raise failure

    try:
        with monkeypatch.context() as fault:
            fault.setattr(worker_channel._Receiver, "revoke", close_then_fail)
            with pytest.raises(PermissionError) as caught:
                handle.close()
        assert caught.value is failure and caught.value.handle is handle
        for descriptor in owned_fds:
            with pytest.raises(OSError) as error:
                os.fstat(descriptor)
            assert error.value.errno == errno.EBADF
    finally:
        with pytest.raises(PermissionError) as repeated:
            handle.close()
        assert repeated.value is failure and not handle.closed


@pytest.mark.parametrize("close_before_error", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_received_descriptor_cleanup_remains_uncertain_without_numeric_retry(
    kernel_boundary, monkeypatch, close_before_error, cancelled
):
    import fcntl
    import stat

    handle = launch(
        LaunchSpec(
            "import os,socket,struct,array,signal; "
            "s=socket.socket(fileno=int(os.environ['HF_CHANNEL_FD'])); "
            "f=os.open('/work/synthetic-right',os.O_CREAT|os.O_RDWR,0o600); "
            "s.sendmsg([struct.pack('!4sQII',b'HFCP',1,0,1)+b'x'],"
            "[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[f,f]))]); "
            "print('ready',flush=True);signal.pause()",
            runtime=kernel_boundary,
        ),
        control_channel=True,
    )
    original_close = os.close
    problem = (
        KeyboardInterrupt("controlled received descriptor cancellation")
        if cancelled
        else PermissionError(errno.EACCES, "controlled received descriptor cleanup refusal")
    )
    retained = []
    sentinel = None

    def identity(descriptor):
        info = os.fstat(descriptor)
        return info.st_dev, info.st_ino, info.st_mode

    try:
        assert handle.process.stdout.readline() == b"ready\n"
        existing = set()
        for value in os.listdir("/proc/self/fd"):
            try:
                os.fstat(int(value))
                existing.add(int(value))
            except OSError:
                pass

        def refusing_close(descriptor):
            if descriptor not in existing and not retained and stat.S_ISREG(os.fstat(descriptor).st_mode):
                retained.append((descriptor, identity(descriptor)))
                if close_before_error:
                    original_close(descriptor)
                raise problem
            original_close(descriptor)

        with monkeypatch.context() as fault:
            fault.setattr(os, "close", refusing_close)
            with pytest.raises(KeyboardInterrupt if cancelled else FleetError) as received:
                handle.control_channel.receive(timeout=3)
            if cancelled:
                assert received.value is problem
        assert len(retained) == 1
        remaining = []
        for value in os.listdir("/proc/self/fd"):
            descriptor = int(value)
            if descriptor not in existing:
                try:
                    if identity(descriptor) == retained[0][1]:
                        remaining.append(descriptor)
                except OSError:
                    pass
        assert remaining == ([] if close_before_error else [retained[0][0]])
        with pytest.raises(FleetError, match="revoked"):
            handle.control_channel.receive(timeout=3)
        assert handle.terminate(3).cleanup_confirmed
        with pytest.raises(type(problem)) as caught:
            handle.close()
        assert caught.value is problem and caught.value.handle is handle
        assert not handle.closed
        descriptor, expected = retained[0]
        if not close_before_error:
            assert identity(descriptor) == expected
            original_close(descriptor)
        # Independent rescue closed the old resource; reusing its number grants no retry.
        sentinel = os.open("/dev/null", os.O_RDONLY)
        if sentinel != descriptor:
            duplicate = fcntl.fcntl(sentinel, fcntl.F_DUPFD_CLOEXEC, descriptor)
            original_close(sentinel)
            sentinel = duplicate
        assert sentinel == descriptor
        witness = identity(sentinel)
        with pytest.raises(type(problem)):
            handle.close()
        assert identity(sentinel) == witness
    finally:
        if retained:
            expected = retained[0][1]
            for value in os.listdir("/proc/self/fd"):
                descriptor = int(value)
                if descriptor not in existing and descriptor != sentinel:
                    try:
                        if identity(descriptor) == expected:
                            original_close(descriptor)
                    except OSError:
                        pass
        if sentinel is not None:
            original_close(sentinel)
        if not handle.closed:
            assert handle.terminate(3).cleanup_confirmed
            try:
                handle.close()
            except (PermissionError, KeyboardInterrupt) as error:
                assert error is problem


@pytest.mark.parametrize("close_before_error", [False, True])
@pytest.mark.parametrize("kind", [PermissionError, KeyboardInterrupt])
def test_stream_cleanup_failure_stays_uncertain_after_independent_cleanup(
    kernel_boundary, monkeypatch, close_before_error, kind
):
    handle = launch(LaunchSpec("pass", runtime=kernel_boundary), control_channel=True)
    assert handle.wait(3).cleanup_confirmed
    original = handle.process.stdout
    primary = kind("controlled stream cleanup failure")

    class FaultedStream:
        def __init__(self):
            self.failed = False

        def close(self):
            if self.failed:
                original.close()
                return
            self.failed = True
            if close_before_error:
                original.close()
            raise primary

    handle.process.stdout = FaultedStream()
    try:
        with pytest.raises(kind) as caught:
            handle.close()
        assert caught.value is primary and primary.handle is handle
        assert handle.process.stderr.closed
        original.close()
        sentinel = os.open("/dev/null", os.O_RDONLY)
        try:
            stamp = os.fstat(sentinel)
            with pytest.raises(kind) as repeated:
                handle.close()
            assert repeated.value is primary and not handle.closed
            assert os.fstat(sentinel) == stamp
        finally:
            os.close(sentinel)
    finally:
        original.close()


def test_preinit_launch_refusal_retains_unconfirmed_process_cleanup(kernel_boundary, monkeypatch):
    from harnessfleet.boundary import ExecutionHandle

    original = ExecutionHandle._pin_launcher
    primary = RuntimeError("controlled pre-init startup cancellation")
    handle = None

    def fail(handle):
        original(handle)
        raise primary

    try:
        with monkeypatch.context() as fault:
            fault.setattr(ExecutionHandle, "_pin_launcher", fail)
            with pytest.raises(RuntimeError) as caught:
                launch(
                    LaunchSpec("print('forbidden-effect',flush=True)", runtime=kernel_boundary),
                    control_channel=True,
                )
        handle = caught.value.handle
        assert caught.value is primary and primary.cleanup_uncertain
        assert not primary.execution_may_have_started
    finally:
        if handle is None:
            handle = getattr(primary, "handle", None)
        if handle is not None:
            handle.reconcile_startup(3)
            assert handle.terminate(3).cleanup_confirmed
            assert b"forbidden-effect" not in handle.process.stdout.read()
            handle.close()
