"""Linux process identity receipts and pidfd-only signaling.

Adapted from the tracked author-owned snapshot recorded in NOTICE.
Numeric PIDs and process groups alone never authorize a signal.
"""

import ctypes
import os
import select
import signal
import sys
import time
from pathlib import Path
from .contracts import FleetError


def pidfd_open(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    lib = ctypes.CDLL(None, use_errno=True)
    fd = lib.pidfd_open(pid, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open failed")
    return fd


def signal_fd(fd, number):
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, number)
    else:
        lib = ctypes.CDLL(None, use_errno=True)
        if lib.pidfd_send_signal(fd, number, None, 0) < 0:
            raise OSError(ctypes.get_errno(), "pidfd_send_signal failed")


def require_linux():
    if sys.platform != "linux":
        raise FleetError("platform", "Execution requires Linux/WSL; macOS is not qualified")
    try:
        fd = pidfd_open(os.getpid())
        os.close(fd)
    except (OSError, AttributeError):
        raise FleetError("platform", "Linux pidfds are required")


def birth(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        if fields[0] == "Z":
            return None
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def receipt(pid):
    return {"pid": pid, "birth": birth(pid)}


def alive(record):
    if not record:
        return False
    try:
        return birth(record["pid"]) == record["birth"] and record["birth"] is not None
    except PermissionError:
        return True  # unreadable ownership never proves exit


def descendant(pid, root):
    handles = []
    try:
        if not root or root.get("birth") is None:
            return False
        chain = []
        seen = set()
        current = pid
        while current > 1 and current not in seen and len(seen) < 256:
            handles.append(pidfd_open(current))
            stamp = receipt(current)
            if stamp["birth"] is None:
                return False
            fields = Path(f"/proc/{current}/stat").read_text().rsplit(") ", 1)[1].split()
            parent = int(fields[1])
            chain.append((stamp, parent))
            if current == root["pid"]:
                if stamp != root or select.select(handles, [], [], 0)[0]:
                    return False
                for record, expected_parent in chain:
                    fields = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(") ", 1)[1].split()
                    if int(fields[1]) != expected_parent or receipt(record["pid"]) != record:
                        return False
                return not select.select(handles, [], [], 0)[0]
            seen.add(current)
            current = parent
        return False
    except (OSError, ValueError, IndexError):
        return False
    finally:
        for handle in handles:
            os.close(handle)


def terminate(run, include_supervisor=False):
    marker = f"HARNESSFLEET_EXECUTION={run['id']}:{run['generation']}:{run['execution_token']}".encode()
    deadline = time.monotonic() + 3
    stable_empty = 0
    seen_zombies = set()
    while time.monotonic() < deadline:
        descriptors = []
        uncertain = False
        changed = False
        try:
            for record in run.get("owned_processes", []):
                fd = None
                try:
                    fd = pidfd_open(record["pid"])
                    if receipt(record["pid"]) == record and record.get("birth") is not None:
                        if not select.select([fd], [], [], 0)[0]:
                            descriptors.append(fd)
                            fd = None
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError:
                    uncertain = True
                finally:
                    if fd is not None:
                        os.close(fd)
            for p in Path("/proc").iterdir():
                if not p.name.isdecimal():
                    continue
                pid = int(p.name)
                if pid == os.getpid() or (
                    not include_supervisor and pid == (run.get("supervisor") or {}).get("pid")
                ):
                    continue
                fd = None
                group = None
                try:
                    fields = (p / "stat").read_text().rsplit(") ", 1)[1].split()
                    group = int(fields[2])
                    if fields[0] == "Z":
                        if group == run.get("pgid") and (pid, fields[19]) not in seen_zombies:
                            seen_zombies.add((pid, fields[19]))
                            changed = True
                        continue
                    # Pin before checking environment: PID reuse cannot redirect a signal.
                    fd = pidfd_open(pid)
                    if marker in (p / "environ").read_bytes().split(b"\0"):
                        descriptors.append(fd)
                        fd = None
                    elif group == run.get("pgid"):
                        # A live group member with a scrubbed marker is not owned
                        # enough to signal, and is not evidence of an empty group.
                        uncertain = True
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError:
                    # Other users' processes are irrelevant; inaccessible members of our
                    # recorded group remain unresolved. Detached inaccessible processes
                    # cannot be proven by this backend (documented containment limit).
                    if group == run.get("pgid"):
                        uncertain = True
                finally:
                    if fd is not None:
                        os.close(fd)
            if not descriptors and not uncertain and not changed:
                stable_empty += 1
                if stable_empty >= 3:
                    return True
            else:
                stable_empty = 0
            for fd in descriptors:
                try:
                    signal_fd(fd, signal.SIGKILL if deadline - time.monotonic() < 1 else signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if descriptors:
                select.select(descriptors, [], [], 0.03)
            else:
                time.sleep(0.02)
        finally:
            for fd in descriptors:
                os.close(fd)
    return False


class Changes:
    """Block on directory write notifications; no repeated unchanged-log reads."""

    def __init__(self, path):
        lib = ctypes.CDLL(None, use_errno=True)
        self.fd = lib.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise FleetError("events", "inotify is unavailable")
        if lib.inotify_add_watch(self.fd, os.fsencode(path), 0x2 | 0x8 | 0x100 | 0x80) < 0:
            os.close(self.fd)
            raise FleetError("events", "Cannot watch execution events")

    def wait(self, timeout):
        if select.select([self.fd], [], [], timeout)[0]:
            while True:
                try:
                    os.read(self.fd, 65536)
                except BlockingIOError:
                    break

    def close(self):
        os.close(self.fd)
