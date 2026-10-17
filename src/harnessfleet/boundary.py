"""Internal, disconnected namespace-launcher experiment. No adapter support implied.

Only explicit hash-pinned runtime snapshots are mounted. Writable paths are ephemeral.
There are no host project, credential, artifact or broker mounts and no networking.
"""

from dataclasses import dataclass, field, replace
import ctypes
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import stat
import struct
import subprocess
import platform
import time

from . import processes
from .contracts import FleetError
from .runtime import RuntimeManifest, prepare_runtime
from .worker_channel import _Receiver

READY = b"harnessfleet-boundary-ready-v1\n"
BOOTSTRAP = """
import os,sys
os.write(1,b'harnessfleet-boundary-ready-v1\\n')
if os.read(0,1)!=b'\\x01':raise SystemExit(125)
fd=os.open('/dev/null',os.O_RDONLY)
os.dup2(fd,0)
os.close(fd)
os.execve('/usr/bin/python3',['python3','-I','-S','-c',sys.argv[1]],dict(os.environ))
"""


@dataclass(frozen=True)
class LaunchSpec:
    code: str
    runtime: RuntimeManifest
    environment: dict[str, str] = field(default_factory=dict)
    network: str = "none"
    version: int = 1


@dataclass(frozen=True)
class ExitEvidence:
    returncode: int | None
    cleanup_confirmed: bool


def _deadline(timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise FleetError("boundary_timeout", "A finite positive boundary timeout is required")
    if not math.isfinite(timeout) or timeout <= 0:
        raise FleetError("boundary_timeout", "A finite positive boundary timeout is required")
    return time.monotonic() + timeout


def _trusted_path(path, *, directory=False):
    """Only the system administrator may replace the runtime or launcher."""
    resolved = path.resolve(strict=True)
    for component in (resolved, *resolved.parents):
        info = component.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise FleetError("boundary_manifest", "Boundary manifest requires administrator-owned runtime")
    info = resolved.stat()
    if (directory and not stat.S_ISDIR(info.st_mode)) or (
        not directory and (not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111)
    ):
        raise FleetError("boundary_manifest", "Invalid runtime in boundary manifest")
    if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise FleetError("boundary_manifest", "Privileged executables are excluded from boundary manifest")
    return resolved


def _validate(spec):
    if (
        spec.version != 1
        or type(spec.version) is not int
        or spec.network != "none"
        or not isinstance(spec.code, str)
        or "\0" in spec.code
        or len(spec.code) > 128 * 1024
        or not isinstance(spec.environment, dict)
        or len(spec.environment) > 64
    ):
        raise FleetError("boundary_manifest", "Unsupported boundary manifest")
    for key, value in spec.environment.items():
        if (
            not isinstance(key, str)
            or not key.isascii()
            or not key.isidentifier()
            or not key.startswith("HF_")
            or not isinstance(value, str)
            or "\0" in value
            or len(value) > 4096
        ):
            raise FleetError("boundary_manifest", "Unsupported environment in boundary manifest")
    try:
        return str(_trusted_path(Path("/usr/bin/bwrap")))
    except (OSError, ValueError):
        raise FleetError("boundary_manifest", "Unavailable launcher in boundary manifest") from None


def _seccomp():
    # Linux x86_64 audit architecture and syscall ABI; compat/x32 execution is excluded.
    rules = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xC000003E), (0x06, 0, 0, 0x80000000)]
    rules += [(0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000)]
    for syscall in (101, 272, 308, 310, 311, 312, 438):
        rules += [(0x15, 0, 1, syscall), (0x06, 0, 0, 0x00050000 | errno.EPERM)]
    rules.append((0x06, 0, 0, 0x7FFF0000))
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = libc.memfd_create(b"harnessfleet-boundary", 0x0001 | 0x0002)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "Boundary filter creation failed")
    try:
        os.write(descriptor, b"".join(struct.pack("HBBI", *rule) for rule in rules))
        fcntl.fcntl(
            descriptor,
            1033,  # F_ADD_SEALS, Linux UAPI, including Python builds omitting the constant.
            0x0008 | 0x0004 | 0x0002 | 0x0001,
        )
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class ExecutionHandle:
    """Own pinned host identities; neither namespace PIDs nor worker markers authorize signals.

    stdout/stderr are pipes the caller must drain with its own bounded-output policy.
    Keep the handle on uncertainty. close() requires confirmed exit of both identities.
    """

    def __init__(self, process, info_fd, block_fd, *, control_channel=None, guest=None):
        self.process = process
        self.launcher = None
        self.launcher_fd = None
        self.namespace_init = None
        self.native_process = None
        self.native_fd = None
        self.init_fd = None
        self.namespace_fd = None
        self.info_fd = info_fd
        self.startup_info = None
        self.block_fd = block_fd
        self.closed = False
        self._control_channel = control_channel
        self._channel_guest = guest
        self._exit_evidence = None
        self._uncertain_fds = []
        self._resource_failure = None
        self._uncertain_resources = []

    @property
    def control_channel(self):
        return self._control_channel

    def _close_resource(self, resource, close):
        if any(resource is retained for retained in self._uncertain_resources):
            return
        try:
            close()
        except BaseException as error:
            self._uncertain_resources.append(resource)
            if self._resource_failure is None:
                self._resource_failure = error
            raise

    def _revoke_channel(self):
        errors = []
        for endpoint in (self._control_channel, self._channel_guest):
            if endpoint is not None:
                try:
                    close = endpoint.revoke if endpoint is self._control_channel else endpoint.close
                    self._close_resource(endpoint, close)
                except BaseException as error:
                    errors.append(error)
        return errors

    def _close_descriptor(self, name):
        descriptor = getattr(self, name)
        setattr(self, name, None)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException as error:
                # A failed close may already have consumed the FD. Never retry its number.
                self._retain_descriptor_failure(descriptor, error)
                raise

    def _retain_descriptor_failure(self, descriptor, error):
        self._uncertain_fds.append(descriptor)
        if self._resource_failure is None:
            self._resource_failure = error

    def _pin_launcher(self):
        if self.launcher is None:
            self.launcher = processes.receipt(self.process.pid)
        descriptor = processes.pidfd_open(self.process.pid)
        try:
            if (
                not self.launcher["birth"]
                or processes.receipt(self.process.pid) != self.launcher
                or select.select([descriptor], [], [], 0)[0]
            ):
                raise FleetError("boundary_identity", "Launcher identity could not be pinned")
            self.launcher_fd = descriptor
            descriptor = None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def reconcile_startup(self, timeout):
        """Pin the original blocked launch after an ownership-read failure; never release it."""
        deadline = _deadline(timeout)
        if self.closed or self.block_fd is None or self.info_fd is None:
            raise FleetError("boundary_startup", "No blocked startup is available for reconciliation")
        if self.launcher_fd is None:
            self._pin_launcher()
        if self.init_fd is None:
            if self.startup_info is None:
                self.startup_info = _information(self.info_fd, deadline)
            self._pin_init(self.startup_info)

    def _pin_init(self, info):
        pid = info.get("child-pid")
        if type(pid) is not int or pid <= 1:
            raise FleetError("boundary_identity", "Invalid namespace initialization receipt")
        fd = processes.pidfd_open(pid)
        namespace_fd = None
        try:
            record = processes.receipt(pid)
            base = Path(f"/proc/{pid}")
            if (
                not record["birth"]
                or not processes.descendant(pid, self.launcher)
                or select.select([fd, self.launcher_fd], [], [], 0)[0]
            ):
                raise FleetError("boundary_identity", "Namespace identity could not be pinned")
            for name in ("pid", "user", "mnt", "net", "ipc", "uts"):
                stamp = (base / "ns" / name).stat().st_ino
                if stamp == Path(f"/proc/self/ns/{name}").stat().st_ino:
                    raise FleetError("boundary_identity", "Required namespace separation is absent")
                if name != "user" and stamp != info.get(name + "-namespace"):
                    raise FleetError("boundary_identity", "Namespace receipt does not match host evidence")
            nspid = next(
                line for line in (base / "status").read_text().splitlines() if line.startswith("NSpid:")
            )
            if nspid.split()[-1] != "1":
                raise FleetError("boundary_identity", "Receipt is not the namespace init process")
            namespace_fd = os.open(base / "ns/pid", os.O_RDONLY | os.O_CLOEXEC)
            if processes.receipt(pid) != record or select.select([fd], [], [], 0)[0]:
                raise FleetError("boundary_identity", "Namespace identity changed during startup")
            self.namespace_init, self.init_fd, self.namespace_fd = record, fd, namespace_fd
            fd, namespace_fd = None, None
        finally:
            for descriptor in (fd, namespace_fd):
                if descriptor is not None:
                    os.close(descriptor)

    def _pin_native(self, deadline):
        data = bytearray()
        descriptor = self.process.stdout.fileno()
        while (
            len(data) < len(READY)
            and select.select([descriptor], [], [], max(0, deadline - time.monotonic()))[0]
        ):
            chunk = os.read(descriptor, len(READY) - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if data != READY:
            raise FleetError("boundary_startup", "Trusted bootstrap did not reach its execution gate")
        init = self.namespace_init["pid"]
        children = Path(f"/proc/{init}/task/{init}/children").read_text().split()
        if len(children) != 1:
            raise FleetError("boundary_identity", "Bootstrap ancestry is ambiguous")
        pid = int(children[0])
        descriptor = processes.pidfd_open(pid)
        try:
            record = processes.receipt(pid)
            if (
                not record["birth"]
                or not processes.descendant(pid, self.namespace_init)
                or Path(f"/proc/{pid}/ns/pid").stat().st_ino != os.fstat(self.namespace_fd).st_ino
                or select.select([descriptor, self.init_fd, self.launcher_fd], [], [], 0)[0]
            ):
                raise FleetError("boundary_identity", "Native bootstrap identity could not be pinned")
            self.native_process, self.native_fd = record, descriptor
            descriptor = None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _evidence(self):
        if self._exit_evidence is not None:
            return self._exit_evidence
        ended = (
            not self.closed and self.init_fd is not None and bool(select.select([self.init_fd], [], [], 0)[0])
        )
        launcher_ended = self.process.poll() is not None
        native_ended = self.native_fd is None or bool(select.select([self.native_fd], [], [], 0)[0])
        return ExitEvidence(self.process.returncode, bool(ended and launcher_ended and native_ended))

    def wait(self, timeout):
        deadline = _deadline(timeout)
        if self.closed:
            raise FleetError("boundary_closed", "Boundary handle is closed")
        if (
            self.init_fd is not None
            and not select.select([self.init_fd], [], [], max(0, deadline - time.monotonic()))[0]
        ):
            return self._evidence()
        try:
            self.process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        return self._evidence()

    def terminate(self, timeout):
        errors = self._revoke_channel()
        try:
            evidence = self._terminate(timeout)
        except BaseException as error:
            error.handle = self
            for secondary in errors:
                error.add_note("Channel revocation also failed: " + type(secondary).__name__)
            raise
        if errors:
            error = errors[0]
            error.handle = self
            for secondary in errors[1:]:
                error.add_note("Channel revocation also failed: " + type(secondary).__name__)
            raise error
        return evidence

    def _terminate(self, timeout):
        deadline = _deadline(timeout)
        if self.closed:
            raise FleetError("boundary_closed", "Boundary handle is closed")
        if self._exit_evidence is not None or self.init_fd is None:
            return self._evidence()
        errors = []
        for fd in (self.init_fd, self.launcher_fd):
            if fd is not None:
                try:
                    if not select.select([fd], [], [], 0)[0]:
                        processes.signal_fd(fd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    errors.append(error)
        evidence = self.wait(max(0.001, deadline - time.monotonic()))
        if errors:
            raise errors[0]
        return evidence

    def close(self):
        errors = self._revoke_channel()
        if self.closed:
            if errors:
                error = errors[0]
                error.handle = self
                raise error
            return
        try:
            evidence = self._evidence()
        except BaseException as error:
            error.handle = self
            for secondary in errors:
                error.add_note("Channel revocation also failed: " + type(secondary).__name__)
            raise
        if not evidence.cleanup_confirmed:
            error = FleetError("boundary_uncertain", "Boundary cleanup remains unconfirmed; retain handle")
            error.handle = self
            for secondary in errors:
                error.add_note("Channel revocation also failed: " + type(secondary).__name__)
            raise error
        self._exit_evidence = evidence
        for name in ("init_fd", "launcher_fd", "native_fd", "namespace_fd", "info_fd", "block_fd"):
            try:
                self._close_descriptor(name)
            except BaseException as error:
                errors.append(error)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    self._close_resource(stream, stream.close)
                except BaseException as error:
                    errors.append(error)
        if self._resource_failure is not None and self._resource_failure not in errors:
            errors.append(self._resource_failure)
        if errors:
            error = errors[0]
            error.handle = self
            error.uncertain_descriptors = tuple(self._uncertain_fds)
            for secondary in errors[1:]:
                error.add_note("Owned resource cleanup also failed: " + type(secondary).__name__)
            raise error
        self.closed = True


def _information(fd, deadline):
    data = bytearray()
    while len(data) < 8192 and select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
        chunk = os.read(fd, 8192 - len(data))
        if not chunk:
            break
        data.extend(chunk)
        try:
            info = json.loads(data)
            if isinstance(info, dict):
                return info
            break
        except (ValueError, UnicodeError):
            continue
    raise FleetError("boundary_startup", "Namespace startup receipt was not available")


def _command(spec, launcher, mounts, info_write, block_read, seccomp):
    command = [
        launcher,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-net",
        "--unshare-uts",
        "--disable-userns",
        "--assert-userns-disabled",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--dir",
        "/usr/bin",
        "--symlink",
        "python3.12",
        "/usr/bin/python3",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/proc/1",
        "--seccomp",
        str(seccomp),
    ]
    for guest, descriptor in mounts:
        command += ["--perms", "0555", "--ro-bind-data", str(descriptor), guest]
    for directory in ("/tmp", "/home", "/run", "/work"):
        command += ["--tmpfs", directory]
    command += ["--chdir", "/work", "--clearenv"]
    for key, value in {
        "HOME": "/home",
        "TMPDIR": "/tmp",
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        **spec.environment,
    }.items():
        command += ["--setenv", key, value]
    command += [
        "--info-fd",
        str(info_write),
        "--block-fd",
        str(block_read),
        "--",
        "/usr/bin/python3",
        "-I",
        "-S",
        "-c",
        BOOTSTRAP,
        spec.code,
    ]
    return command


def launch(spec: LaunchSpec, *, startup_timeout=5, control_channel=False) -> ExecutionHandle:
    deadline = _deadline(startup_timeout)
    launcher = _validate(spec)
    if type(control_channel) is not bool or control_channel and "HF_CHANNEL_FD" in spec.environment:
        raise FleetError("boundary_channel", "Unsupported channel option or reserved environment")
    processes.require_linux()
    if platform.machine() != "x86_64":
        raise FleetError("boundary_platform", "Boundary syscall policy requires Linux x86_64")
    prepared = prepare_runtime(spec.runtime)
    info_read = info_write = block_read = block_write = seccomp = None
    handle = None
    receiver = guest = host = process = None
    primary = None
    grant_attempted = False
    try:
        if control_channel:
            host, guest = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            host.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            guest.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            receiver = _Receiver(host)
            host = None
            spec = replace(spec, environment={**spec.environment, "HF_CHANNEL_FD": str(guest.fileno())})
            _validate(spec)
        info_read, info_write = os.pipe2(os.O_CLOEXEC)
        block_read, block_write = os.pipe2(os.O_CLOEXEC)
        seccomp = _seccomp()
        command = _command(spec, launcher, prepared.mounts, info_write, block_read, seccomp)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            close_fds=True,
            pass_fds=(
                info_write,
                block_read,
                seccomp,
                *((guest.fileno(),) if guest is not None else ()),
                *(descriptor for _, descriptor in prepared.mounts),
            ),
            start_new_session=True,
        )
        handle = ExecutionHandle(process, info_read, block_write, control_channel=receiver, guest=guest)
        receiver = guest = None
        # Transfer the startup gate before any identity syscall can fail.
        info_read, block_write = None, None
        handle._pin_launcher()
        handle.reconcile_startup(max(0.001, deadline - time.monotonic()))
        # No payload executes until the host pins the distinct launcher and trusted init identities.
        os.write(handle.block_fd, b"x")
        handle._close_descriptor("block_fd")
        handle._close_descriptor("info_fd")
        handle._pin_native(deadline)
        if handle.control_channel is not None:
            handle.control_channel._bind(handle)
            handle._close_resource(handle._channel_guest, handle._channel_guest.close)
            handle._channel_guest = None
        # EOF on Bubblewrap's setup gate is permissive. This second gate requires a byte,
        # so supervisor death during setup never grants payload execution.
        grant_attempted = True
        handle.process.stdin.write(b"\x01")
        handle._close_resource(handle.process.stdin, handle.process.stdin.close)
        handle.process.stdin = None
    except BaseException as error:
        primary = error

    cleanup_errors = []
    uncertain_fds = []
    uncertain_endpoints = []
    for endpoint in (host, guest, receiver):
        if endpoint is not None:
            try:
                endpoint.revoke() if endpoint is receiver else endpoint.close()
            except BaseException as error:
                uncertain_endpoints.append(endpoint)
                cleanup_errors.append(error)
    try:
        prepared.close()
    except BaseException as error:
        cleanup_errors.append(error)
    for descriptor in (info_read, info_write, block_read, block_write, seccomp):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException as error:
                uncertain_fds.append(descriptor)
                cleanup_errors.append(error)
    cleanup_uncertain = bool(cleanup_errors or uncertain_fds or uncertain_endpoints)
    process_cleanup_confirmed = process is None
    if primary is None and cleanup_errors:
        primary = cleanup_errors.pop(0)
    if primary is not None:
        if handle is not None:
            cleanup_errors.extend(handle._revoke_channel())
            if handle.init_fd is not None:
                try:
                    process_cleanup_confirmed = handle.terminate(1).cleanup_confirmed
                except BaseException as error:
                    cleanup_errors.append(error)
        error = primary
        if isinstance(primary, (OSError, FleetError, StopIteration)):
            error = FleetError("boundary_startup", "Namespace startup failed; no automatic retry")
        error.handle = handle
        error.unregistered_process = process if handle is None else None
        error.startup_resources = prepared
        error.startup_descriptors = tuple(uncertain_fds)
        error.startup_endpoints = tuple(uncertain_endpoints)
        error.execution_may_have_started = grant_attempted
        error.cleanup_uncertain = bool(
            cleanup_uncertain
            or cleanup_errors
            or not process_cleanup_confirmed
            or prepared.mounts
            or prepared.temporary
            or prepared._close_error
            or handle is not None
            and handle._resource_failure
        )
        for secondary in cleanup_errors:
            error.add_note("Startup cleanup also failed: " + type(secondary).__name__)
        if error is primary:
            raise error
        raise error from primary
    return handle
