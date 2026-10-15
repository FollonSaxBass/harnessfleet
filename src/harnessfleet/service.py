"""Optional local queue/recovery controller. No system service is installed."""

import json
import os
import secrets
import signal
import subprocess
import sys
from . import processes
from .contracts import FleetError


def controller_once(manager):
    launch = []
    with manager.store.transaction() as c:
        for run in manager.store.all(c):
            if run["status"] == "starting" and not run.get("parent_run") and not run.get("spawn_claim"):
                run["spawn_claim"] = True
                manager.store.save(c, run, "controller_spawn_claimed")
                launch.append(run)
    for run in launch:
        manager._spawn(run, _from_broker=True)
    recovered = manager.recover()
    dispatched = manager.dispatch()
    resumed = []
    for run in manager.store.all():
        if run["status"] != "quota" or not run.get("auto_remaining", 0) or not run.get("cleanup_confirmed"):
            continue
        a = manager.adapters[run["harness"]]
        if not a.capabilities.quota_recovery or not callable(getattr(a, "quota_ready", None)):
            continue
        # Adapter must prove readiness for this exact account and Route. A quota
        # reset hint alone is insufficient authorization or readiness evidence.
        ready = a.quota_ready(dict(run))
        if ready is not True:
            continue
        try:
            resumed.append(
                manager.resume(
                    run["id"],
                    "Continue the bounded assignment after quota recovery.",
                    expected_generation=run["generation"],
                    _automatic=True,
                )
            )
        except FleetError:
            pass  # no native effect; another controller or policy may have won
    return {"schema_version": 1, "recovery": recovered, "queue": dispatched, "continued": resumed}


def read(manager, c=None):
    if c is None:
        with manager.store.transaction() as c:
            return read(manager, c)
    row = c.execute("SELECT data FROM meta WHERE key='service'").fetchone()
    return json.loads(row[0]) if row else None


def status(manager):
    data = read(manager)
    return {
        "schema_version": 1,
        "running": bool(data and processes.alive(data.get("process"))),
        "process": data.get("process") if data else None,
    }


def start(manager):
    processes.require_linux()
    with manager.store.transaction() as c:
        previous = read(manager, c)
        if previous and processes.alive(previous.get("process")):
            raise FleetError("service", "Controller is already running")
        token = secrets.token_hex(24)
        env = dict(
            os.environ,
            HARNESSFLEET_CONFIG=str(manager.config_path),
            HARNESSFLEET_STATE=str(manager.store.root),
            HARNESSFLEET_SERVICE_TOKEN=token,
        )
        p = subprocess.Popen(
            [sys.executable, "-m", "harnessfleet", "service", "run"],
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        data = {"token": token, "process": processes.receipt(p.pid)}
        c.execute("INSERT OR REPLACE INTO meta VALUES ('service',?)", (json.dumps(data),))
    return status(manager)


def stop(manager):
    processes.require_linux()
    with manager.store.transaction() as c:
        data = read(manager, c)
        if not data or not processes.alive(data.get("process")):
            return {"schema_version": 1, "running": False}
        pid = data["process"]["pid"]
        fd = processes.pidfd_open(pid)
        try:
            from pathlib import Path

            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            if (
                not processes.alive(data["process"])
                or f"HARNESSFLEET_SERVICE_TOKEN={data['token']}".encode() not in env
            ):
                raise FleetError("service", "Controller process ownership is uncertain")
            processes.signal_fd(fd, signal.SIGTERM)
        finally:
            os.close(fd)
    return {"schema_version": 1, "stop_requested": True}


def serve(manager):
    processes.require_linux()
    if (
        os.environ.get("HARNESSFLEET_BOX")
        or os.environ.get("CODEX_BOX_ACCOUNT")
        or os.environ.get("CURSOR_BOX_ACCOUNT")
    ):
        raise FleetError("box_origin", "Boxes cannot originate managed execution controllers")
    token = os.environ.get("HARNESSFLEET_SERVICE_TOKEN")
    process = processes.receipt(os.getpid())

    def registered():
        # The registration transaction in start finishes before this read.
        current = read(manager)
        return bool(
            token
            and current
            and secrets.compare_digest(current["token"], token)
            and current["process"] == process
        )

    if not registered():
        raise FleetError("service", "Controller registration is missing or obsolete")
    watch = processes.Changes(manager.store.root)
    try:
        while True:
            if not registered():
                return
            controller_once(manager)
            watch.wait(30)
    finally:
        watch.close()
