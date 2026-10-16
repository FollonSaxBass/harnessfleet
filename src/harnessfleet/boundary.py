"""Internal, disconnected namespace-launcher experiment. No adapter support implied.

Only a system /usr runtime is mounted, read-only. All writable paths are ephemeral.
There are no host project, credential, artifact or broker mounts and no networking.
"""

from dataclasses import dataclass, field
import ctypes
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import select
import signal
import stat
import struct
import subprocess
import platform
import time

from . import processes
from .contracts import FleetError

READY = b"harnessfleet-boundary-ready-v1\n"
BOOTSTRAP = """
import os,sys
os.write(1,b'harnessfleet-boundary-ready-v1\\n')
if os.read(0,1)!=b'\\x01':raise SystemExit(125)
fd=os.open('/dev/null',os.O_RDONLY)
os.dup2(fd,0)
os.close(fd)
os.execve(sys.argv[1],sys.argv[1:],dict(os.environ))
"""


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    runtime_root: str
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
        or spec.runtime_root != "/usr"
        or spec.network != "none"
        or not isinstance(spec.argv, tuple)
        or not spec.argv
        or any(not isinstance(value, str) or "\0" in value for value in spec.argv)
        or sum(map(len, spec.argv)) > 256 * 1024
        or not Path(spec.argv[0]).is_absolute()
        or not Path(spec.argv[0]).is_relative_to("/usr")
        or ".." in Path(spec.argv[0]).parts
        or not isinstance(spec.environment, dict)
        or len(spec.environment) > 64
    ):
        raise FleetError("boundary_manifest", "Unsupported boundary manifest")
    for key, value in spec.environment.items():
        if (
            not isinstance(key, str)
            or not key.isascii()
            or not key.isidentifier()
            or (key not in {"LANG", "LC_ALL", "TZ"} and not key.startswith("HF_"))
            or not isinstance(value, str)
            or "\0" in value
            or len(value) > 4096
        ):
            raise FleetError("boundary_manifest", "Unsupported environment in boundary manifest")
    try:
        runtime = _trusted_path(Path(spec.runtime_root), directory=True)
        executable = _trusted_path(Path(spec.argv[0]))
        _trusted_path(Path("/usr/bin/python3"))
        launcher = _trusted_path(Path("/usr/bin/bwrap"))
        if runtime != Path("/usr") or not executable.is_relative_to(runtime):
            raise FleetError("boundary_manifest", "Executable is outside boundary runtime manifest")
        for library in ("/usr/lib", "/usr/lib64", "/usr/bin"):
            if not Path(library).resolve(strict=True).is_relative_to(runtime):
                raise FleetError("boundary_manifest", "Unsupported runtime layout in boundary manifest")
    except (OSError, ValueError):
        raise FleetError("boundary_manifest", "Unavailable runtime in boundary manifest") from None
    return str(launcher)


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

    def __init__(self, process, info_fd, block_fd):
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
        deadline = _deadline(timeout)
        if self.closed:
            raise FleetError("boundary_closed", "Boundary handle is closed")
        if self.init_fd is None:
            return self._evidence()
        for fd in (self.init_fd, self.launcher_fd):
            if fd is not None and not select.select([fd], [], [], 0)[0]:
                try:
                    processes.signal_fd(fd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        return self.wait(max(0.001, deadline - time.monotonic()))

    def close(self):
        if self.closed:
            return
        if not self._evidence().cleanup_confirmed:
            error = FleetError("boundary_uncertain", "Boundary cleanup remains unconfirmed; retain handle")
            error.handle = self
            raise error
        for fd in (
            self.init_fd,
            self.launcher_fd,
            self.native_fd,
            self.namespace_fd,
            self.info_fd,
            self.block_fd,
        ):
            if fd is not None:
                os.close(fd)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
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


def launch(spec: LaunchSpec, *, startup_timeout=5) -> ExecutionHandle:
    deadline = _deadline(startup_timeout)
    launcher = _validate(spec)
    processes.require_linux()
    if platform.machine() != "x86_64":
        raise FleetError("boundary_platform", "Boundary syscall policy requires Linux x86_64")
    info_read = info_write = block_read = block_write = seccomp = None
    handle = None
    try:
        info_read, info_write = os.pipe2(os.O_CLOEXEC)
        block_read, block_write = os.pipe2(os.O_CLOEXEC)
        seccomp = _seccomp()
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
            "--ro-bind",
            "/usr",
            "/usr",
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
        for directory in ("/tmp", "/home", "/run", "/work"):
            command += ["--tmpfs", directory]
        command += ["--chdir", "/work", "--clearenv"]
        for key, value in {
            "HOME": "/home",
            "TMPDIR": "/tmp",
            "PATH": "/usr/bin:/bin",
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
            "-c",
            BOOTSTRAP,
            *spec.argv,
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            close_fds=True,
            pass_fds=(info_write, block_read, seccomp),
            start_new_session=True,
        )
        handle = ExecutionHandle(process, info_read, block_write)
        # Transfer the startup gate before any identity syscall can fail.
        info_read, block_write = None, None
        handle._pin_launcher()
        os.close(info_write)
        info_write = None
        os.close(block_read)
        block_read = None
        handle.reconcile_startup(max(0.001, deadline - time.monotonic()))
        # No payload executes until the host pins the distinct launcher and trusted init identities.
        os.write(handle.block_fd, b"x")
        os.close(handle.block_fd)
        handle.block_fd = None
        os.close(handle.info_fd)
        handle.info_fd = None
        handle._pin_native(deadline)
        # EOF on Bubblewrap's setup gate is permissive. This second gate requires a byte,
        # so supervisor death during setup never grants payload execution.
        handle.process.stdin.write(b"\x01")
        handle.process.stdin.close()
        handle.process.stdin = None
        return handle
    except (OSError, FleetError, StopIteration) as cause:
        error = FleetError("boundary_startup", "Namespace startup failed; no automatic retry")
        error.handle = handle
        if handle is not None and handle.init_fd is not None:
            try:
                handle.terminate(1)
            except (OSError, FleetError):
                error.cleanup_uncertain = True
        raise error from cause
    finally:
        for descriptor in (info_read, info_write, block_read, block_write, seccomp):
            if descriptor is not None:
                os.close(descriptor)
