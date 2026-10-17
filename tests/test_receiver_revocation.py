"""Direct and handle-driven revocation share retained endpoint ownership."""

import errno
import fcntl
import os
import select
import signal
import socket

import pytest

from harnessfleet import FleetError, boundary, processes, worker_channel
from harnessfleet.boundary import LaunchSpec, launch
from test_boundary import kernel_boundary as kernel_boundary


@pytest.mark.parametrize("entry", ["direct", "terminate", "close"])
@pytest.mark.parametrize("close_before_error", [False, True])
@pytest.mark.parametrize("kind", [PermissionError, KeyboardInterrupt])
def test_endpoint_revocation_failure_is_shared_and_never_retried(
    kernel_boundary, monkeypatch, entry, close_before_error, kind
):
    code = "import signal;signal.pause()" if entry == "terminate" else "pass"
    handle = launch(LaunchSpec(code, runtime=kernel_boundary), control_channel=True)
    pins = [os.dup(fd) for fd in (handle.launcher_fd, handle.init_fd, handle.native_fd)]
    endpoint = handle.control_channel._endpoint
    target = endpoint.fileno()
    stamp = os.fstat(target)
    close = socket.socket.close
    problem = kind("controlled endpoint close failure")
    sentinel = None
    attempts = []

    def closing(sock):
        if sock is endpoint:
            attempts.append(True)
            if close_before_error:
                close(sock)
            raise problem
        close(sock)

    try:
        if entry != "terminate":
            assert handle.wait(3).cleanup_confirmed
        with monkeypatch.context() as fault:
            fault.setattr(socket.socket, "close", closing)
            with pytest.raises(kind) as caught:
                if entry == "direct":
                    handle.control_channel.revoke()
                elif entry == "terminate":
                    handle.terminate(3)
                else:
                    handle.close()
        assert caught.value is problem
        assert handle._resource_failure is problem
        assert attempts == [True]
        if close_before_error:
            sentinel = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
            if sentinel != target:
                with pytest.raises(OSError) as missing:
                    os.fstat(target)
                assert missing.value.errno == errno.EBADF
                duplicate = fcntl.fcntl(sentinel, fcntl.F_DUPFD_CLOEXEC, target)
                os.close(sentinel)
                sentinel = duplicate
            assert sentinel == target
            stamp = os.fstat(sentinel)
        for _ in range(2):
            with pytest.raises(kind) as revoked:
                handle.control_channel.revoke()
            assert revoked.value is problem
            with pytest.raises(kind) as refused:
                handle.close()
            assert refused.value is problem and refused.value.handle is handle
            assert not handle.closed
            assert os.fstat(target) == stamp
        with pytest.raises(FleetError, match="revoked"):
            handle.control_channel.receive(timeout=1)
    finally:
        for descriptor in pins:
            if not select.select([descriptor], [], [], 0)[0]:
                processes.signal_fd(descriptor, signal.SIGKILL)
        handle.process.wait(timeout=3)
        for descriptor in pins:
            assert select.select([descriptor], [], [], 3)[0]
            os.close(descriptor)
        try:
            handle.close()
        except BaseException as error:
            assert error is problem
        if sentinel is not None:
            assert os.fstat(sentinel) == stamp
            os.close(sentinel)
        elif endpoint.fileno() != -1:
            assert os.fstat(endpoint.fileno()) == stamp
            close(endpoint)


@pytest.mark.parametrize("close_before_error", [False, True])
@pytest.mark.parametrize("kind", [PermissionError, KeyboardInterrupt])
def test_unbound_receiver_retains_startup_close_error(kernel_boundary, monkeypatch, close_before_error, kind):
    captured = []
    original = worker_channel._Receiver.__init__
    close = socket.socket.close
    primary = RuntimeError("controlled pre-process failure")
    problem = kind("controlled unbound endpoint failure")

    def receiver(self, endpoint):
        original(self, endpoint)
        captured.append((self, endpoint, os.fstat(endpoint.fileno())))

    def closing(endpoint):
        if captured and endpoint is captured[0][1]:
            if close_before_error:
                close(endpoint)
            raise problem
        close(endpoint)

    def spawn(*args, **kwargs):
        raise primary

    try:
        with monkeypatch.context() as fault:
            fault.setattr(worker_channel._Receiver, "__init__", receiver)
            fault.setattr(boundary.subprocess, "Popen", spawn)
            fault.setattr(socket.socket, "close", closing)
            with pytest.raises(RuntimeError) as caught:
                launch(LaunchSpec("pass", runtime=kernel_boundary), control_channel=True)
        assert caught.value is primary and primary.cleanup_uncertain
        retained, endpoint, stamp = captured[0]
        assert retained in primary.startup_endpoints
        for _ in range(2):
            with pytest.raises(kind) as revoked:
                retained.revoke()
            assert revoked.value is problem
        with pytest.raises(FleetError):
            retained._bind(None)
        with pytest.raises(FleetError, match="revoked"):
            retained.receive(timeout=1)
    finally:
        if captured:
            _, endpoint, stamp = captured[0]
            if endpoint.fileno() != -1:
                assert os.fstat(endpoint.fileno()) == stamp
                close(endpoint)
