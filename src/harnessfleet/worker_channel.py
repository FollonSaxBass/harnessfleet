"""Internal receive-only observation channel. It grants no execution authority."""

import array
import math
import os
from pathlib import Path
import select
import socket
import struct
import time

from . import processes
from .contracts import FleetError
from .worker_frames import MAX_FRAME_SIZE, ProtocolError, decode_frame

_CREDENTIALS = struct.Struct("3i")


def _refused(message):
    return FleetError("boundary_channel", message)


class _Receiver:
    def __init__(self, endpoint):
        self._endpoint = endpoint
        self._handle = None
        self._writer = None
        self._namespace = None
        self._uid, self._gid = os.getuid(), os.getgid()
        self._revoked = False
        self._sequence = 0
        self._attempts = 0
        self._close_error = None
        self._closed = False

    def _bind(self, handle):
        if self._handle is not None or self._revoked:
            raise _refused("Channel binding is unavailable")
        self._handle = handle
        self._writer = dict(handle.native_process)
        self._namespace = os.fstat(handle.namespace_fd).st_ino
        if not self._live():
            raise _refused("Original writer is unavailable")

    def _live(self):
        if self._revoked or self._handle is None or self._handle.closed:
            return False
        handle = self._handle
        try:
            return (
                self._writer["birth"] is not None
                and not select.select([handle.native_fd, handle.init_fd, handle.launcher_fd], [], [], 0)[0]
                and processes.receipt(self._writer["pid"]) == self._writer
                and Path(f"/proc/{self._writer['pid']}/ns/pid").stat().st_ino == self._namespace
            )
        except (OSError, TypeError, ValueError):
            return False

    def revoke(self):
        self._revoked = True
        if self._close_error is not None:
            raise self._close_error
        handle = self._handle
        if handle is not None and any(self is resource for resource in handle._uncertain_resources):
            raise handle._resource_failure
        if self._closed:
            return
        try:
            self._endpoint.close()
            self._closed = True
        except BaseException as error:
            self._close_error = error
            if handle is not None:
                handle._retain_resource_failure(self, error)
            raise

    def receive(self, *, timeout):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise _refused("A finite positive channel timeout is required")
        deadline = time.monotonic() + timeout
        if not self._live():
            raise _refused("Original writer is inactive or revoked")
        if self._attempts >= 16:
            raise _refused("Channel record budget exhausted")
        handle = self._handle
        try:
            ready = select.select(
                [self._endpoint, handle.native_fd, handle.init_fd, handle.launcher_fd],
                [],
                [],
                max(0, deadline - time.monotonic()),
            )[0]
            if not ready:
                raise _refused("Channel receive deadline elapsed")
            if not self._live():
                raise _refused("Original writer is inactive or revoked")
            data, controls, flags, _ = self._endpoint.recvmsg(
                MAX_FRAME_SIZE,
                socket.CMSG_SPACE(_CREDENTIALS.size) + socket.CMSG_SPACE(16),
                socket.MSG_CMSG_CLOEXEC | socket.MSG_DONTWAIT,
            )
        except OSError as error:
            raise _refused("Channel receive is unavailable") from error
        self._attempts += 1
        credentials = []
        rights = set()
        invalid = False
        for level, kind, raw in controls:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array.array("i")
                values.frombytes(raw[: len(raw) - len(raw) % values.itemsize])
                rights.update(values)
                invalid = True
            elif (
                level == socket.SOL_SOCKET
                and kind == socket.SCM_CREDENTIALS
                and len(raw) == _CREDENTIALS.size
            ):
                credentials.append(_CREDENTIALS.unpack(raw))
            else:
                invalid = True
        close_errors = []
        for descriptor in sorted(rights):
            try:
                os.close(descriptor)
            except BaseException as error:
                handle._retain_descriptor_failure(descriptor, error)
                close_errors.append(error)
        if close_errors:
            self._revoked = True
            primary = next(
                (error for error in close_errors if not isinstance(error, OSError)), close_errors[0]
            )
            error = primary
            if isinstance(primary, OSError):
                error = _refused("Received descriptor cleanup remains unconfirmed; retain handle")
            error.handle = handle
            error.uncertain_descriptors = tuple(handle._uncertain_fds)
            for secondary in close_errors:
                if secondary is not primary:
                    error.add_note("Received descriptor cleanup also failed: " + type(secondary).__name__)
            if error is primary:
                raise error
            raise error from primary
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or invalid or len(credentials) != 1:
            raise _refused("Channel controls or truncation refused")
        if credentials[0] != (self._writer["pid"], self._uid, self._gid) or not self._live():
            raise _refused("Channel writer refused")
        if not data:
            raise _refused("Channel reached EOF")
        try:
            body = decode_frame(data, generation=1, sequence=self._sequence)
        except ProtocolError as error:
            raise _refused("Channel frame refused") from error
        if not self._live():
            raise _refused("Original writer is inactive or revoked")
        self._sequence += 1
        return body
