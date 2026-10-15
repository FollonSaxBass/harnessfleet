"""Detached per-generation supervisor. No native invocation is ever retried here."""

import hashlib
import json
import os
import subprocess
import sys
import time
from .contracts import ACTIVE, FleetError, Result
from .manager import RunManager
from . import processes
from .storage import MAX_ARTIFACT


def redact(text, env=None):
    import re

    value = str(text)
    for k, v in (env or os.environ).items():
        if (
            any(s in k.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "EXECUTION"))
            and len(v) >= 8
        ):
            value = value.replace(v, "[redacted]")
    return re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,})\b", "[redacted]", value)


def native_lines(process, stderr_chunks):
    """Read bounded protocol/stderr streams until the native pidfd signals exit."""
    import selectors

    monitor = selectors.DefaultSelector()
    output_fd = process.stdout.fileno()
    error_fd = process.stderr.fileno()
    for fd in (output_fd, error_fd):
        os.set_blocking(fd, False)
    death = processes.pidfd_open(process.pid)
    monitor.register(output_fd, selectors.EVENT_READ, "output")
    monitor.register(error_fd, selectors.EVENT_READ, "stderr")
    monitor.register(death, selectors.EVENT_READ, "exit")
    pending = b""
    error_size = 0
    exited = False
    open_streams = {output_fd, error_fd}
    try:
        while True:
            events = monitor.select()
            exited = exited or any(key.data == "exit" for key, _ in events)
            for fd in list(open_streams):
                while True:
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        monitor.unregister(fd)
                        open_streams.remove(fd)
                        break
                    if fd == error_fd:
                        error_size += len(chunk)
                        if error_size > MAX_ARTIFACT:
                            raise FleetError("protocol", "Native stderr exceeded bounded limits")
                        stderr_chunks.append(chunk.decode("utf-8", errors="replace"))
                        continue
                    pending += chunk
                    if len(pending) > 1024 * 1024:
                        raise FleetError("protocol", "Native line exceeded bounded limits")
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        yield line + b"\n"
            if exited:
                if pending:
                    yield pending
                return
    finally:
        monitor.close()
        os.close(death)


def serve_children(manager, parent_id, generation, stopped):
    """Host-origin launch broker, independent of the native tool's process lifetime."""
    watch = processes.Changes(manager.store.root)
    try:
        while not stopped.is_set():
            launch = []
            with manager.store.transaction() as c:
                parent = manager.store.get(parent_id, c)
                if parent["generation"] != generation or parent["status"] != "running":
                    return
                for child in manager.store.all(c):
                    if (child.get("parent_run"), child.get("parent_generation")) != (parent_id, generation):
                        continue
                    if child["status"] == "starting" and not child.get("spawn_claim"):
                        child["spawn_claim"] = True
                        manager.store.save(c, child, "host_spawn_claimed")
                        launch.append(child)
            for child in launch:
                manager._spawn(child, _from_broker=True)
            watch.wait(30)
    finally:
        watch.close()


def supervise(rid, generation):
    m = RunManager()
    s = m.store
    run = s.get(rid)
    marker = f"{rid}:{generation}:{run['execution_token']}"
    if run["generation"] != generation or os.environ.get("HARNESSFLEET_EXECUTION") != marker:
        return 2
    import threading

    stopped = threading.Event()
    process = None
    result = Result()
    raw = []
    failure = None
    native_env = {}
    portal = None
    stderr_chunks = []
    try:
        directory = s.directory(rid, generation)
        with s.transaction() as c:
            run = s.get(rid, c)
            if run["generation"] != generation or run["status"] != "starting":
                return 2
            run["supervisor"] = processes.receipt(os.getpid())
            run["pgid"] = os.getpgrp()
            route, adapter = m._validate(run, m.configuration(), dispatch=True)
            is_box = run["environment"].startswith("box:")
            readiness = {"available": True} if is_box else adapter.diagnose(route)
            if not readiness.get("available"):
                raise FleetError("authentication", "Native prerequisite or authentication is unavailable")
            native_env = adapter.environment(route, os.environ)
            native_env.update(
                HARNESSFLEET_PARENT_RUN=rid,
                HARNESSFLEET_PARENT_GENERATION=str(generation),
                HARNESSFLEET_PARENT_TOKEN=run["child_token"],
                HARNESSFLEET_EXECUTION=marker,
                AGENT_IDENTITY=run["identity"],
            )
            for key in (
                "HARNESSFLEET_CONFIG",
                "HARNESSFLEET_STATE",
                "HARNESSFLEET_SERVICE_TOKEN",
                "HARNESSFLEET_BROKER",
                "HARNESSFLEET_BROKER_OWNER",
            ):
                native_env.pop(key, None)
            # The skill is a bounded explicit reference, not a parent conversation export.
            from importlib.resources import files

            skill = str(files("harnessfleet").joinpath("skill/SKILL.md"))
            run["assignment"] = (
                f"Execution identity: {run['identity']}. "
                f"For permitted delegation read the common skill at {skill}. "
                "Never change billing or substitute an explicitly requested target.\n\n" + run["assignment"]
            )
            if is_box:
                # Box accounts, config and executable paths belong to the image.
                command_run = {
                    **run,
                    "route": {
                        **route,
                        "executable": route["box"].get("native_executable", adapter.executable),
                    },
                }
                command = adapter.command(command_run, str(directory))
            else:
                command = adapter.command(run, str(directory))
                from .delegation import Portal

                portal = Portal(m, rid, generation)
                native_env.update(portal.environment())
            # Receipt is consumed before Popen; a crash here is ambiguous, never replayable.
            run.update(invoked=True, status="running", updated_at=time.time())
            s.save(c, run, "invocation_consumed")
        # Commit the consumed marker before any native/container effect.
        if is_box:
            from .boxes import prepare

            def persist_box():
                with s.transaction() as c:
                    current = s.get(rid, c)
                    if current["generation"] != generation or current["status"] != "running":
                        raise FleetError("generation", "Container creation was fenced")
                    current["box_receipt"] = run["box_receipt"]
                    s.save(c, current, "container_receipt")

            def container_effect(action):
                with s.transaction() as c:
                    current = s.get(rid, c)
                    if current["generation"] != generation or current["status"] != "running":
                        raise FleetError("generation", "Container effect was fenced")
                    return action()

            command = prepare(run, command, persist_box, container_effect)
        with s.transaction() as c:
            current = s.get(rid, c)
            if current["generation"] != generation or current["status"] != "running":
                raise FleetError("generation", "Native creation was fenced")
            process = subprocess.Popen(
                command,
                cwd=run["cwd"],
                env=native_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
            current["native_process"] = processes.receipt(process.pid)
            if portal is not None:
                portal.bind_native(current["native_process"])
            s.save(c, current, "native_created")
        broker = threading.Thread(target=serve_children, args=(m, rid, generation, stopped), daemon=True)
        broker.start()
        size = 0
        for line in native_lines(process, stderr_chunks):
            size += len(line)
            if len(line) > 1024 * 1024 or size > 16 * 1024 * 1024:
                raise FleetError("protocol", "Native event stream exceeded bounded limits")
            text = line.decode("utf-8", errors="replace")
            raw.append(text)
            try:
                event = json.loads(text)
            except ValueError:
                raise FleetError("protocol", "Native output is not the declared JSONL protocol")
            if not isinstance(event, dict):
                raise FleetError("protocol", "Native event is not an object")
            adapter.consume(event, result)
            if len(result.output.encode()) > MAX_ARTIFACT:
                raise FleetError("protocol", "Native final output exceeds the bounded limit")
            with s.transaction() as c:
                current = s.get(rid, c)
                if current["generation"] != generation or current["status"] != "running":
                    raise FleetError("generation", "Supervisor was fenced")
                if result.session_id:
                    current["session_id"] = result.session_id
                current["updated_at"] = time.time()
                s.save(
                    c,
                    current,
                    "progress",
                    {"type": str(event.get("type", event.get("event", "unknown")))[:80]},
                )
        code = process.wait()
        if code or not result.completed or not result.output.strip() or not result.session_id:
            failure = result.failure or "no_final_output"
        else:
            failure = result.failure
    except (Exception, KeyboardInterrupt) as e:
        failure = e.code if isinstance(e, FleetError) else "execution"
    finally:
        stopped.set()
        if portal is not None:
            portal.close()
        # Child exit does not prove descendant cleanup. Keep capacity on uncertainty.
        current = s.get(rid)
        if current["generation"] != generation:
            return 2
        remote_clean = m._cleanup_box(current)
        clean = processes.terminate(current) and remote_clean
        if process is not None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                clean = False
        if stderr_chunks:
            s.write_artifact(rid, generation, "stderr.txt", redact("".join(stderr_chunks), native_env))
        if raw:
            s.write_artifact(rid, generation, "events.jsonl", redact("".join(raw), native_env))
        if result.output:
            s.write_artifact(rid, generation, "partial.txt", redact(result.output, native_env))
        with s.transaction() as c:
            current = s.get(rid, c)
            if current["generation"] != generation or current["status"] not in ACTIVE:
                return 2
            accepted = bool(
                not failure and clean and result.completed and result.output and result.session_id
            )
            state = (
                "finished" if accepted else (failure if failure in ("quota", "authentication") else "failed")
            )
            if current["status"] == "stopping":
                state = "stopped"
                accepted = False
            if not clean:
                state = "uncertain"
                accepted = False
            if accepted:
                output = redact(result.output, native_env)
                s.write_artifact(rid, generation, "output.txt", output)
                current["output_sha256"] = hashlib.sha256(output.encode()).hexdigest()
            current.update(
                status=state,
                accepted=accepted,
                cleanup_confirmed=clean,
                failure=failure,
                session_id=result.session_id or current.get("session_id"),
                reset_at=result.reset_at,
                updated_at=time.time(),
            )
            receipt = {
                "schema_version": 1,
                "id": rid,
                "generation": generation,
                "invoked": current["invoked"],
                "accepted": accepted,
                "cleanup_confirmed": clean,
                "output_sha256": current.get("output_sha256"),
            }
            s.write_artifact(rid, generation, "receipt.json", json.dumps(receipt))
            s.save(c, current, "completed", {"status": state, "accepted": accepted})
        from .notifications import notify

        notify(m, current)
    return 0 if current.get("accepted") else 1


if __name__ == "__main__":
    raise SystemExit(supervise(sys.argv[1], int(sys.argv[2])))
