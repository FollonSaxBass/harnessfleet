"""Kernel evidence for the internal launcher, separate from adapter qualification."""

import json
import errno
import os
import platform
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import stat
import sys
import uuid

import pytest

from harnessfleet import FleetError, processes
from harnessfleet.boundary import LaunchSpec, launch
from harnessfleet.runtime import RuntimeManifest
from runtime_fixture import pinned_runtime


@pytest.fixture(scope="session")
def kernel_boundary():
    manifest = pinned_runtime()
    if manifest is None:
        pytest.skip("No explicit hash-pinned runtime fixture supplied; kernel backend is unqualified")
    if sys.platform != "linux" or platform.machine() != "x86_64" or not shutil.which("bwrap"):
        pytest.skip("Linux x86_64 and rootless Bubblewrap are required for kernel qualification")
    if not shutil.which("unshare") or Path("/usr/bin/bwrap").stat().st_mode & stat.S_ISUID:
        pytest.skip("This qualification requires non-setuid Bubblewrap and rootless namespaces")
    probe = subprocess.run(
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--pid",
            "--mount",
            "--ipc",
            "--net",
            "--uts",
            "--fork",
            "/usr/bin/true",
        ],
        capture_output=True,
        timeout=5,
    )
    if probe.returncode:
        pytest.skip("Kernel/container policy disables the required rootless namespaces")
    help_text = subprocess.check_output(["/usr/bin/bwrap", "--help"], text=True)
    if "--assert-userns-disabled" not in help_text:
        pytest.skip("Bubblewrap lacks the required user-namespace restriction")
    handle = launch(LaunchSpec("pass", runtime=manifest))
    try:
        assert handle.wait(5).cleanup_confirmed
    finally:
        assert handle.terminate(5).cleanup_confirmed
        handle.close()
    return manifest


@pytest.mark.parametrize(
    "change",
    [
        {"code": None},
        {"code": "\0"},
        {"code": "x" * (128 * 1024 + 1)},
        {"network": "host"},
        {"version": 2},
        {"environment": {"HOME": "/host"}},
        {"environment": {"LD_PRELOAD": "/usr/lib/override.so"}},
        {"environment": {"LANG": "custom"}},
    ],
)
def test_unsupported_manifest_is_rejected_before_payload(change, tmp_path):
    marker = tmp_path / "must-not-exist"
    spec = {"code": f"open({str(marker)!r},'w').write('effect')", "runtime": RuntimeManifest(())}
    spec.update(change)
    with pytest.raises(FleetError, match="manifest"):
        launch(LaunchSpec(**spec))
    assert not marker.exists()


def test_real_kernel_denies_host_state_process_ipc_and_fds(kernel_boundary, tmp_path, monkeypatch):
    secret = tmp_path / "controller-state"
    secret.write_text("synthetic-controller-token")
    monkeypatch.setenv("HF_AMBIENT_SECRET", "synthetic-ambient-token")
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    sock = socket.socket(socket.AF_UNIX)
    address = "\0hf-boundary-" + uuid.uuid4().hex
    sock.bind(address)
    sock.listen()
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen()
    fd = os.open(secret, os.O_RDONLY)
    os.set_inheritable(fd, True)
    payload = r"""
import ctypes,json,os,socket,sys
from pathlib import Path
target=Path(TARGET)
def denied(action):
    try: action();return False
    except OSError:return True
lib=ctypes.CDLL(None,use_errno=True)
class IOV(ctypes.Structure):
    _fields_=[('base',ctypes.c_void_p),('size',ctypes.c_size_t)]
source=ctypes.c_long(123)
destination=ctypes.c_long()
remote=IOV(ctypes.addressof(source),ctypes.sizeof(source))
local=IOV(ctypes.addressof(destination),ctypes.sizeof(destination))
vm_denied=lib.process_vm_readv(os.getpid(),ctypes.byref(local),1,ctypes.byref(remote),1,0)==-1 and ctypes.get_errno()==1
def connect(address):
    with socket.socket(socket.AF_UNIX) as s:s.connect(address)
def tcp_connect():
    with socket.socket() as s:s.settimeout(1);s.connect(('127.0.0.1',PORT))
Path('/work/positive').write_text('allowed')
os.symlink(TARGET,'/work/alias')
result={
 'host_read_denied':denied(target.read_text),
 'host_write_denied':denied(lambda:target.write_text('corrupt')),
 'alias_read_denied':denied(lambda:Path('/work/alias').read_text()),
 'host_proc_denied':denied(lambda:Path('/proc/HOSTPID/environ').read_bytes()),
 'host_root_denied':denied(lambda:Path('/proc/HOSTPID/root'+TARGET).read_bytes()),
 'host_signal_denied':denied(lambda:os.kill(HOSTPID,0)),
 'host_pidfd_denied':denied(lambda:os.pidfd_open(HOSTPID)),
 'host_abstract_socket_denied':denied(lambda:connect(ABSTRACT)),
 'host_path_socket_denied':denied(lambda:connect(TARGET+'.sock')),
 'host_loopback_denied':denied(tcp_connect),
 'ambient_secret_absent':'HF_AMBIENT_SECRET' not in os.environ,
 'host_fd_absent':denied(lambda:os.fstat(HOSTFD)),
 'nested_userns_denied':lib.unshare(0x10000000)==-1,
 'host_ptrace_denied':lib.ptrace(16,HOSTPID,0,0)==-1,
 'native_pid_is_two':os.getpid()==2,
 'init_proc_hidden':denied(lambda:Path('/proc/1/mem').read_bytes()),
 'init_ptrace_denied':lib.ptrace(16,1,0,0)==-1,
 'process_vm_filter_enforced':vm_denied,
 'positive_work':Path('/work/positive').read_text()=='allowed',
 'runtime_read':Path('/usr/bin/python3').exists(),
 'no_site':'site' not in sys.modules and 'sitecustomize' not in sys.modules,
 'narrow_runtime':not any(Path(p).exists() for p in ('/usr/share','/usr/bin/true','/etc/ld.so.cache','/usr/lib/python3.12/site-packages')),
 'stdlib_from_zip':__import__('json').__file__.startswith('/usr/lib/python312.zip/'),
 'fixed_locale':os.environ.get('LC_ALL')=='C' and os.environ.get('TZ')=='UTC',
 'explicit_environment':os.environ.get('HF_BOUNDARY_CASE')=='explicit',
 'capabilities_dropped':next(x for x in Path('/proc/self/status').read_text().splitlines() if x.startswith('CapEff:')).split()[1]=='0000000000000000',
 'no_new_privileges':next(x for x in Path('/proc/self/status').read_text().splitlines() if x.startswith('NoNewPrivs:')).split()[1]=='1',
}
print(json.dumps(result),flush=True)
"""
    payload = payload.replace("TARGET", repr(str(secret))).replace("HOSTPID", str(sentinel.pid))
    payload = payload.replace("HOSTFD", str(fd)).replace("ABSTRACT", repr(address))
    payload = payload.replace("PORT", str(tcp.getsockname()[1]))
    path_sock = socket.socket(socket.AF_UNIX)
    path_sock.bind(str(secret) + ".sock")
    path_sock.listen()
    handle = None
    try:
        handle = launch(
            LaunchSpec(
                payload,
                runtime=kernel_boundary,
                environment={"HF_BOUNDARY_CASE": "explicit"},
            )
        )
        stdout, stderr = handle.process.communicate(timeout=10)
        evidence = handle.wait(5)
        assert evidence.cleanup_confirmed and evidence.returncode == 0, stderr
        observations = json.loads(stdout)
        assert all(observations.values()), observations
        assert secret.read_text() == "synthetic-controller-token"
        assert sentinel.poll() is None
        assert not select.select([sock, path_sock, tcp], [], [], 0)[0]
        assert not (tmp_path / "positive").exists()
    finally:
        if handle:
            assert handle.terminate(5).cleanup_confirmed
            handle.close()
        os.close(fd)
        sock.close()
        tcp.close()
        path_sock.close()
        sentinel.terminate()
        sentinel.wait(timeout=5)


@pytest.mark.parametrize("action", ["terminate", "launcher_death", "init_death", "payload_death"])
def test_namespace_death_cleans_detached_reexec_without_environment_marker(kernel_boundary, action):
    payload = r"""
import ctypes,os,sys,time
assert ctypes.CDLL(None).prctl(1,0,0,0,0)==0
if os.fork()==0:
    os.setsid()
    os.execve('/usr/bin/python3',['python3','-I','-c',"import time;print('ready',flush=True);time.sleep(60)"],{})
time.sleep(60)
"""
    handle = launch(LaunchSpec(payload, runtime=kernel_boundary))
    child_fd = None
    try:
        assert select.select([handle.process.stdout], [], [], 5)[0]
        assert handle.process.stdout.readline() == b"ready\n"
        init = handle.namespace_init
        children = Path(f"/proc/{init['pid']}/task/{init['pid']}/children").read_text().split()
        assert children == [str(handle.native_process["pid"])]
        native = handle.native_process["pid"]
        children = Path(f"/proc/{native}/task/{native}/children").read_text().split()
        assert len(children) == 1
        child_fd = processes.pidfd_open(int(children[0]))
        assert not select.select([child_fd], [], [], 0)[0]
        assert handle.launcher != handle.namespace_init
        assert handle.native_process not in (handle.launcher, handle.namespace_init)
        if action == "terminate":
            evidence = handle.terminate(5)
        else:
            descriptor = {
                "launcher_death": handle.launcher_fd,
                "init_death": handle.init_fd,
                "payload_death": handle.native_fd,
            }[action]
            processes.signal_fd(descriptor, signal.SIGKILL)
            evidence = handle.wait(5)
        assert evidence.cleanup_confirmed
        assert select.select([child_fd], [], [], 5)[0]
    finally:
        assert handle.terminate(5).cleanup_confirmed
        handle.close()
        if child_fd is not None:
            os.close(child_fd)


@pytest.mark.parametrize("phase", ["launcher", "namespace"])
def test_ownership_failure_retains_blocked_payload_until_explicit_reconciliation(
    kernel_boundary, monkeypatch, phase
):
    original = processes.pidfd_open

    def exhausted(pid):
        foreign_namespace = (
            Path(f"/proc/{pid}/ns/pid").stat().st_ino != Path("/proc/self/ns/pid").stat().st_ino
        )
        if pid != os.getpid() and (phase == "launcher" or foreign_namespace):
            raise OSError(errno.EMFILE, "synthetic pidfd exhaustion")
        return original(pid)

    monkeypatch.setattr(processes, "pidfd_open", exhausted)
    with pytest.raises(FleetError) as caught:
        launch(LaunchSpec("print('must-not-execute')", runtime=kernel_boundary))
    handle = caught.value.handle
    assert handle is not None
    try:
        assert handle.process.poll() is None
        assert not handle.terminate(0.05).cleanup_confirmed
        assert handle.process.poll() is None
        assert not select.select([handle.process.stdout], [], [], 0.05)[0]
        with pytest.raises(FleetError, match="unconfirmed"):
            handle.close()
    finally:
        monkeypatch.setattr(processes, "pidfd_open", original)
        handle.reconcile_startup(5)
        assert handle.terminate(5).cleanup_confirmed
        stdout, _ = handle.process.communicate(timeout=5)
        assert stdout == b""
        handle.close()


@pytest.mark.parametrize("phase", ["running", "blocked"])
def test_supervisor_death_kills_trusted_init_and_markerless_descendants(kernel_boundary, phase):
    script = r"""
import errno,json,os,time
from pathlib import Path
from harnessfleet import FleetError,processes
from harnessfleet.boundary import LaunchSpec,launch
from harnessfleet.runtime import RuntimeFile,RuntimeManifest
import hashlib
raw=Path(os.environ["HF_BOUNDARY_RUNTIME_MANIFEST"]).read_bytes()
assert hashlib.sha256(raw).hexdigest()==os.environ["HF_BOUNDARY_RUNTIME_SHA256"]
data=json.loads(raw)
runtime=RuntimeManifest(tuple(RuntimeFile(**f) for f in data["files"]))
phase=PHASE
spec=LaunchSpec("import ctypes,os,time;ctypes.CDLL(None).prctl(1,0,0,0,0);p=os.fork();os.setsid() if p==0 else None;print('ready',flush=True) if p else None;time.sleep(60)",runtime=runtime)
if phase=='blocked':
    original=processes.pidfd_open
    def unavailable(pid):
        if pid!=os.getpid():raise OSError(errno.EMFILE,'synthetic pidfd exhaustion')
        return original(pid)
    processes.pidfd_open=unavailable
    try:launch(spec);raise AssertionError('unexpected launch')
    except FleetError as error:h=error.handle
    finally:processes.pidfd_open=original
    h.reconcile_startup(5)
else:
    h=launch(spec)
    assert h.process.stdout.readline()==b'ready\n'
init=h.namespace_init['pid']
native=h.native_process['pid'] if h.native_process else None
children=Path(f'/proc/{native}/task/{native}/children').read_text().split() if native else []
print(json.dumps({'launcher':h.launcher['pid'],'init':init,'native':native,'children':children}),flush=True)
time.sleep(60)
"""
    script = script.replace("PHASE", repr(phase))
    supervisor = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    parent_fd = processes.pidfd_open(supervisor.pid)
    descriptors = []
    try:
        assert select.select([supervisor.stdout], [], [], 5)[0]
        line = supervisor.stdout.readline()
        assert line, supervisor.stderr.read(4096)
        record = json.loads(line)
        owner = processes.receipt(supervisor.pid)
        pids = [record["launcher"], record["init"], record["native"], *map(int, record["children"])]
        for pid in [pid for pid in pids if pid is not None]:
            assert processes.descendant(pid, owner)
            descriptors.append(processes.pidfd_open(pid))
        assert len(descriptors) == (4 if phase == "running" else 2)
        processes.signal_fd(parent_fd, signal.SIGKILL)
        supervisor.wait(timeout=5)
        for descriptor in descriptors:
            assert select.select([descriptor], [], [], 5)[0]
    finally:
        for descriptor in [parent_fd, *descriptors]:
            if not select.select([descriptor], [], [], 0)[0]:
                processes.signal_fd(descriptor, signal.SIGKILL)
        supervisor.wait(timeout=5)
        for descriptor in [parent_fd, *descriptors]:
            os.close(descriptor)
        supervisor.stdout.close()
        supervisor.stderr.close()


def test_compat_syscall_abi_is_rejected_by_kernel_filter(kernel_boundary):
    handle = launch(
        LaunchSpec(
            "import ctypes;ctypes.CDLL(None).syscall(0x40000027);print('escaped')", runtime=kernel_boundary
        )
    )
    try:
        stdout, _ = handle.process.communicate(timeout=5)
        evidence = handle.wait(5)
        assert evidence.cleanup_confirmed and evidence.returncode != 0
        assert stdout == b""
    finally:
        assert handle.terminate(5).cleanup_confirmed
        handle.close()


def test_identity_read_error_does_not_leak_untransferred_launcher_pidfd(kernel_boundary, monkeypatch):
    original = processes.receipt
    observed = set()

    def target_pidfds(pid):
        result = set()
        for item in Path("/proc/self/fdinfo").iterdir():
            try:
                if f"Pid:\t{pid}\n" in item.read_text():
                    result.add(int(item.name))
            except FileNotFoundError:
                continue
        return result

    def unreadable(pid):
        pinned = target_pidfds(pid)
        if pid != os.getpid() and pinned:
            observed.update(pinned)
            raise PermissionError("synthetic identity access denial after pidfd open")
        return original(pid)

    monkeypatch.setattr(processes, "receipt", unreadable)
    with pytest.raises(FleetError) as caught:
        launch(LaunchSpec("print('must-not-execute')", runtime=kernel_boundary))
    handle = caught.value.handle
    try:
        assert observed
        assert not target_pidfds(handle.process.pid)
    finally:
        monkeypatch.setattr(processes, "receipt", original)
        handle.reconcile_startup(5)
        assert handle.terminate(5).cleanup_confirmed
        handle.close()
        for descriptor in observed & target_pidfds(handle.process.pid):
            os.close(descriptor)
