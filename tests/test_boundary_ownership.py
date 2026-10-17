"""Startup cleanup retains ownership and the original failure before transfer."""

import errno
import os

import pytest

from harnessfleet import boundary, processes
from harnessfleet.boundary import LaunchSpec, launch
from test_boundary import kernel_boundary as kernel_boundary


@pytest.mark.parametrize("phase", ["launcher", "init", "native"])
@pytest.mark.parametrize("close_before_error", [False, True])
@pytest.mark.parametrize("kind", [KeyboardInterrupt, OSError])
def test_failed_pin_cleanup_preserves_cancellation_and_other_descriptors(
    kernel_boundary, monkeypatch, phase, close_before_error, kind
):
    primary = kind("controlled pin cancellation")
    failure = PermissionError(errno.EACCES, "controlled pin cleanup failure")
    captured = {}
    handles = []
    closed = []
    close = os.close
    open_fd = os.open
    pidfd_open = processes.pidfd_open
    receipt = processes.receipt
    method = getattr(boundary.ExecutionHandle, "_pin_" + phase)

    def pin(handle, *args):
        handles.append(handle)

        def pidfd(pid):
            descriptor = pidfd_open(pid)
            if "pidfd" not in captured:
                captured["pidfd"] = descriptor
                captured["stamp"] = os.fstat(descriptor)
            return descriptor

        def opening(path, flags, *args, **kwargs):
            descriptor = open_fd(path, flags, *args, **kwargs)
            if str(path).endswith("/ns/pid"):
                captured["namespace"] = descriptor
                captured["namespace_stamp"] = os.fstat(descriptor)
            return descriptor

        def observe(pid):
            if "pidfd" in captured and (phase != "init" or "namespace" in captured):
                captured["cancelled"] = True
                raise primary
            return receipt(pid)

        def closing(descriptor):
            closed.append(descriptor)
            if captured.get("cancelled") and descriptor == captured["pidfd"]:
                if close_before_error:
                    close(descriptor)
                raise failure
            return close(descriptor)

        with monkeypatch.context() as fault:
            fault.setattr(processes, "pidfd_open", pidfd)
            fault.setattr(processes, "receipt", observe)
            fault.setattr(os, "open", opening)
            fault.setattr(os, "close", closing)
            return method(handle, *args)

    handle = None
    sentinel = None
    try:
        with monkeypatch.context() as fault:
            fault.setattr(boundary.ExecutionHandle, "_pin_" + phase, pin)
            with pytest.raises(BaseException) as caught:
                launch(LaunchSpec("print('forbidden-effect',flush=True)", runtime=kernel_boundary))
        handle = handles[0]
        if close_before_error:
            sentinel = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
            target = captured["pidfd"]
            if sentinel != target:
                with pytest.raises(OSError) as missing:
                    os.fstat(target)
                assert missing.value.errno == errno.EBADF
                os.dup2(sentinel, target)
                close(sentinel)
                sentinel = target
            captured["sentinel_stamp"] = os.fstat(sentinel)
        assert (caught.value.__cause__ if kind is OSError else caught.value) is primary
        assert caught.value.handle is handle and caught.value.cleanup_uncertain
        assert captured["pidfd"] in handle._uncertain_fds
        assert handle._resource_failure is failure
        if phase == "init":
            assert captured["namespace"] in closed
        assert not caught.value.execution_may_have_started
    finally:
        handle = handle or (handles[0] if handles else None)
        if handle is not None:
            if handle.init_fd is None:
                handle.reconcile_startup(3)
            assert handle.terminate(3).cleanup_confirmed
            assert b"forbidden-effect" not in handle.process.stdout.read()
            try:
                handle.close()
            except BaseException as error:
                assert error is failure
        if "pidfd" in captured:
            expected = captured["sentinel_stamp"] if sentinel is not None else captured["stamp"]
            assert os.fstat(captured["pidfd"]) == expected
            close(captured["pidfd"])
        if "namespace" in captured:
            try:
                stamp = os.fstat(captured["namespace"])
            except OSError as error:
                assert error.errno == errno.EBADF
            else:
                assert stamp == captured["namespace_stamp"]
                close(captured["namespace"])


@pytest.mark.parametrize("kind", [KeyboardInterrupt, OSError])
def test_seccomp_constructor_retains_descriptor_and_original_cancellation(kernel_boundary, monkeypatch, kind):
    primary = kind("controlled filter cancellation")
    failure = PermissionError(errno.EACCES, "controlled filter cleanup failure")
    captured = {}
    write, close = os.write, os.close

    def writing(descriptor, data):
        if "memfd:harnessfleet-boundary" in os.readlink(f"/proc/self/fd/{descriptor}"):
            captured.update(descriptor=descriptor, stamp=os.fstat(descriptor))
            raise primary
        return write(descriptor, data)

    def closing(descriptor):
        if descriptor == captured.get("descriptor"):
            raise failure
        return close(descriptor)

    try:
        with monkeypatch.context() as fault:
            fault.setattr(os, "write", writing)
            fault.setattr(os, "close", closing)
            fault.setattr(boundary.subprocess, "Popen", lambda *a, **kw: pytest.fail("process effect"))
            with pytest.raises(BaseException) as caught:
                launch(LaunchSpec("pass", runtime=kernel_boundary))
        assert (caught.value.__cause__ if kind is OSError else caught.value) is primary
        assert caught.value.cleanup_uncertain
        assert caught.value.startup_descriptors == (captured["descriptor"],)
        assert not caught.value.execution_may_have_started and caught.value.handle is None
    finally:
        if "descriptor" in captured:
            assert os.fstat(captured["descriptor"]) == captured["stamp"]
            close(captured["descriptor"])


@pytest.mark.parametrize("kind", [KeyboardInterrupt, OSError, None])
def test_ancestry_cleanup_preserves_cancellation_and_transfers_uncertainty(
    kernel_boundary, monkeypatch, kind
):
    primary = kind("controlled ancestry cancellation") if kind is not None else None
    failure = PermissionError(errno.EACCES, "controlled ancestry cleanup failure")
    handles, pins = [], []
    original_pin = boundary.ExecutionHandle._pin_init
    original_descendant = processes.descendant
    pidfd_open, receipt, close = processes.pidfd_open, processes.receipt, os.close

    def descendant(pid, root):
        def opening(pid):
            descriptor = pidfd_open(pid)
            pins.append((descriptor, os.fstat(descriptor)))
            return descriptor

        def observing(pid):
            if primary is not None and len(pins) == 2:
                raise primary
            return receipt(pid)

        def closing(descriptor):
            if descriptor == pins[0][0]:
                raise failure
            close(descriptor)

        with monkeypatch.context() as fault:
            fault.setattr(processes, "pidfd_open", opening)
            fault.setattr(processes, "receipt", observing)
            fault.setattr(os, "close", closing)
            return original_descendant(pid, root)

    def pin(handle, info):
        handles.append(handle)
        return original_pin(handle, info)

    try:
        with monkeypatch.context() as fault:
            fault.setattr(processes, "descendant", descendant)
            fault.setattr(boundary.ExecutionHandle, "_pin_init", pin)
            with pytest.raises(BaseException) as caught:
                launch(LaunchSpec("print('forbidden-effect',flush=True)", runtime=kernel_boundary))
        expected = primary if primary is not None else failure
        assert (caught.value if kind is KeyboardInterrupt else caught.value.__cause__) is expected
        assert caught.value.cleanup_uncertain and caught.value.handle is handles[0]
        assert pins[0][0] in handles[0]._uncertain_fds
        assert handles[0]._resource_failure is failure
        for descriptor, _ in pins[1:]:
            with pytest.raises(OSError) as closed:
                os.fstat(descriptor)
            assert closed.value.errno == errno.EBADF
    finally:
        if handles:
            handle = handles[0]
            if handle.init_fd is None:
                handle.reconcile_startup(3)
            assert handle.terminate(3).cleanup_confirmed
            assert b"forbidden-effect" not in handle.process.stdout.read()
            try:
                handle.close()
            except BaseException as error:
                assert error is failure
        for descriptor, stamp in pins:
            try:
                current = os.fstat(descriptor)
            except OSError as error:
                assert error.errno == errno.EBADF
            else:
                assert current == stamp
                close(descriptor)
