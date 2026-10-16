"""Public execution contract v1. All native effects follow transactional admission."""

import copy
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from . import adapters, config, processes
from .contracts import ACTIVE, TERMINAL, FleetError
from .storage import Store

PUBLIC = (
    "id",
    "generation",
    "status",
    "route_id",
    "harness",
    "environment",
    "account",
    "billing",
    "model",
    "permission",
    "delegation",
    "delegation_transport",
    "cwd",
    "parent_run",
    "parent_generation",
    "depth",
    "caller",
    "requesting_actor",
    "session_id",
    "created_at",
    "updated_at",
    "cleanup_confirmed",
    "invoked",
    "accepted",
    "failure",
    "reset_at",
    "kept",
    "identity",
    "output_sha256",
    "auto_remaining",
    "source_status",
)
PERMISSIONS = {
    "read-only": {"read-only"},
    "workspace-write": {"read-only", "workspace-write"},
    "delegate": {"read-only", "workspace-write", "delegate"},
    "unrestricted": {"read-only", "workspace-write", "delegate", "unrestricted"},
}


def public(run):
    return {"schema_version": 1, **{k: run.get(k) for k in PUBLIC}}


def inside(path, roots):
    return any(path.is_relative_to(Path(r).expanduser().resolve()) for r in roots)


class RunManager:
    contract_version = 1

    def __new__(cls, config_path=None, state_path=None):
        from .delegation import Client, managed_environment

        if (
            os.environ.get("HARNESSFLEET_BOX")
            or os.environ.get("CODEX_BOX_ACCOUNT")
            or os.environ.get("CURSOR_BOX_ACCOUNT")
        ):
            raise FleetError("box_origin", "Boxes cannot originate managed delegation in v0.1")
        if managed_environment():
            if config_path is not None or state_path is not None:
                raise FleetError("delegation_scope", "Worker clients cannot select instance storage")
            return Client()
        return super().__new__(cls)

    def __init__(self, config_path=None, state_path=None):
        cp, sp = config.locations()
        self.config_path = Path(config_path or cp).absolute()
        self.store = Store(state_path or sp)
        self.adapters = adapters.registry()
        self._principal = None

    def configuration(self):
        return config.load(self.config_path)

    def routes(self, diagnose=False):
        data = self.configuration()
        result = []
        for route in data["routes"]:
            a = self.adapters.get(route["harness"])
            transport = route.get(
                "delegation_transport", data["policy"].get("delegation_transport", "disabled")
            )
            row = {
                k: route.get(k)
                for k in (
                    "id",
                    "harness",
                    "environment",
                    "account",
                    "billing",
                    "models",
                    "default_model",
                    "concurrency",
                )
            }
            row["permitted"] = route["id"] in data["policy"]["routes"]
            row["execution_boundary"] = {
                "delegation_transport": transport
                if route.get("environment", "host") == "host"
                else "unsupported",
                "delegation_qualification": "unqualified",
                "delegation_reason": "Transport disabled; select explicit experimental transport to test"
                if transport == "disabled"
                else (
                    "Owned stdio helper requires separate native launch and account qualification"
                    if transport == "experimental-stdio-mcp-v1"
                    else "Codex CLI 0.162.1 native Unix sandbox connectivity is blocked"
                    if route["harness"] == "codex_cli"
                    else "Experimental transport has no native qualification"
                ),
                "worker_isolation": "unsupported",
                "requested_isolation": route.get(
                    "worker_isolation", data["policy"].get("worker_isolation", "trusted")
                ),
            }
            row["support"] = (
                adapters.describe(a) if a else {"capabilities": {}, "error": "adapter unavailable"}
            )
            if diagnose:
                if route.get("environment", "host").startswith("box:"):
                    from .boxes import diagnose as diagnose_box

                    row["readiness"] = diagnose_box(route)
                else:
                    row["readiness"] = a.diagnose(route) if a else {"available": False}
            result.append(row)
        return {"schema_version": 1, "routes": result}

    def _route(self, rid, cfg):
        found = [r for r in cfg["routes"] if r["id"] == rid]
        if not found:
            raise FleetError("target", "Requested Route is unavailable; no substitution was made")
        return copy.deepcopy(found[0])

    def _validate(self, run, cfg, c, *, dispatch=False):
        route = self._route(run["route_id"], cfg)
        if any(
            boundary.get("worker_isolation", "trusted") != "trusted"
            for boundary in (route, cfg["policy"], run["grant"])
        ):
            raise FleetError(
                "isolation",
                "No worker-isolation backend is qualified; trusted execution cannot satisfy an isolation requirement",
            )
        if dispatch and route != run["route"]:
            raise FleetError(
                "route_changed", "Route changed after admission; submit a new authorized request"
            )
        a = self.adapters.get(route["harness"])
        if not a:
            raise FleetError("adapter", "Requested adapter is not installed")
        cap = a.capabilities
        if not cap.launch:
            raise FleetError("unsupported", "This adapter does not support launch")
        support = cap.platforms.get(sys.platform, "unsupported")
        if support == "unsupported":
            raise FleetError("platform", "This adapter/platform combination is unsupported")
        grants = [cfg["policy"], run["grant"]]
        for grant in grants:
            if route["id"] not in grant["routes"]:
                raise FleetError("policy", "Requested Route is forbidden")
            if run["billing"] not in grant["billing"] or route["billing"] != run["billing"]:
                raise FleetError("billing", "Billing selection is not authorized; no fallback was made")
            if run["permission"] not in grant["permissions"]:
                raise FleetError("permission", "Permission request exceeds inherited authority")
            if grant.get("expires_at", float("inf")) <= time.time():
                raise FleetError("grant", "Delegation grant has expired")
            if run["depth"] > grant["max_depth"]:
                raise FleetError("depth", "Managed Agent Run depth limit exceeded")
            if support == "experimental" and not grant.get("experimental", False):
                raise FleetError("experimental", "This combination requires explicit experimental opt-in")
            if not inside(Path(run["cwd"]).resolve(), grant["cwd_roots"]):
                raise FleetError("cwd", "Working directory is outside the approved grant")
            pairs = grant.get("targets")
            if pairs and {"route": route["id"], "model": run["model"]} not in pairs:
                raise FleetError("target", "Target is outside the inherited selection constraints")
        if run["permission"] not in cap.permissions:
            raise FleetError("permission", "Adapter cannot enforce the requested permission mode")
        if run.get("parent_run"):
            parent = self.store.get(run["parent_run"], c)
            parent_route = self._route(parent["route_id"], cfg)
            if parent_route.get(
                "delegation_transport", cfg["policy"].get("delegation_transport", "disabled")
            ) != parent.get("delegation_transport") or any(
                parent.get("delegation_transport") not in config.authorized_transports(g)
                or parent.get("delegation_transport") not in config.TRANSPORTS - {"disabled"}
                or not g.get("experimental")
                for g in grants
            ):
                raise FleetError(
                    "delegation_transport", "Parent delegation transport authorization was revoked"
                )
        if run.get("delegation", "leaf") == "required":
            transport = route.get(
                "delegation_transport", cfg["policy"].get("delegation_transport", "disabled")
            )
            if (
                transport not in config.TRANSPORTS - {"disabled"}
                or run.get("delegation_transport") != transport
                or route.get("environment", "host") != "host"
                or any(
                    transport not in config.authorized_transports(g) or not g.get("experimental")
                    for g in grants
                )
            ):
                raise FleetError(
                    "delegation_transport",
                    "Required delegation transport is disabled or unavailable; no fallback was made",
                )
            if transport == "experimental-stdio-mcp-v1" and route["harness"] != "codex_cli":
                raise FleetError("delegation_transport", "Managed stdio is experimental for host Codex only")
        if run["model"] not in route.get("models", []):
            raise FleetError(
                "model", "Requested model is unavailable on this Route; no substitution was made"
            )
        if not Path(run["cwd"]).is_dir() or str(Path(run["cwd"]).resolve()) != run["cwd"]:
            raise FleetError("cwd", "Working directory is missing or changed")
        if route.get("environment", "host").startswith("box:"):
            from .boxes import validate

            validate(run)
        if run.get("auto_remaining", 0) and (not cap.quota_recovery or not cap.continuation):
            raise FleetError("quota_recovery", "Adapter does not prove account-specific quota recovery")
        if run["generation"] > 1 and not cap.continuation:
            raise FleetError("continuation", "Adapter does not support continuation")
        return route, a

    def _parent(self, c):
        if (
            os.environ.get("HARNESSFLEET_BOX")
            or os.environ.get("CODEX_BOX_ACCOUNT")
            or os.environ.get("CURSOR_BOX_ACCOUNT")
        ):
            raise FleetError("box_origin", "Boxes cannot originate managed delegation in v0.1")
        authority = self._principal or {
            "id": os.environ.get("HARNESSFLEET_PARENT_RUN"),
            "generation": os.environ.get("HARNESSFLEET_PARENT_GENERATION"),
            "token": os.environ.get("HARNESSFLEET_PARENT_TOKEN", ""),
        }
        rid = authority["id"]
        if not rid:
            return None
        parent = self.store.get(rid, c)
        if parent.get("delegation", "leaf") != "required":
            raise FleetError("parent", "Leaf runs cannot originate delegation")
        if parent["status"] != "running" or not hmac.compare_digest(
            parent["child_token"], authority["token"]
        ):
            raise FleetError("parent", "Parent generation is inactive or authority is invalid")
        if str(parent["generation"]) != str(authority["generation"]):
            raise FleetError("parent", "Parent generation is obsolete")
        if self._principal and not self._principal.get("live", lambda: True)():
            raise FleetError("parent", "Native requesting process is no longer attributable")
        return parent

    def _resource(self, run, c):
        if self._principal:
            parent = self._parent(c)
            if (run.get("parent_run"), run.get("parent_generation")) != (parent["id"], parent["generation"]):
                raise FleetError("delegation_scope", "Run is outside this parent generation")

    def events(self, rid, after=0):
        return {"schema_version": 1, "events": self.store.events(rid, after)}

    def _capacity(self, run, rows, cfg):
        active = [
            r
            for r in rows
            if r["id"] != run["id"] and (r["status"] in ACTIVE or not r.get("cleanup_confirmed", True))
        ]
        route_limit = run["route"].get("concurrency", 1)
        if len([r for r in active if r["route_id"] == run["route_id"]]) >= route_limit:
            return False
        for grant in (cfg["policy"], run["grant"]):
            if len(active) >= grant["max_parallel"]:
                return False
        return True

    def _count_invocation(self, run, rows, cfg):
        count = sum(r.get("invocation_count", 0) for r in rows if r.get("root_run") == run["root_run"])
        if count >= min(cfg["policy"]["max_invocations"], run["grant"]["max_invocations"]):
            raise FleetError("budget", "Delegation invocation budget exhausted")
        run["invocation_count"] = run.get("invocation_count", 0) + 1

    def start(
        self,
        route,
        assignment,
        *,
        cwd=None,
        model=None,
        billing="subscription",
        permission="read-only",
        queue=False,
        selection=None,
        auto_resume=0,
        caller=None,
        delegation="leaf",
    ):
        processes.require_linux()
        if delegation not in ("leaf", "required"):
            raise FleetError("delegation_transport", "Select leaf or required delegation explicitly")
        if not isinstance(assignment, str) or not assignment.strip() or len(assignment) > 65536:
            raise FleetError("assignment", "Assignment must contain 1..65536 characters")
        if type(auto_resume) is not int or not 0 <= auto_resume <= 3:
            raise FleetError("quota_recovery", "Automatic continuation must be finite (0..3)")
        cfg = self.configuration()
        native_route = self._route(route, cfg)
        model = model or native_route.get("default_model")
        if not model:
            raise FleetError("model", "Resolve an explicit model before launch")
        if caller is not None:
            # Exact callers need source attribution; arbitrary user text cannot create edges.
            raise FleetError("caller", "Use a connected lineage source to attribute exact native callers")
        selection = selection or {}
        target = {"route": route, "model": model}
        if selection.get("required") and selection["required"] != [target]:
            raise FleetError("selection", "Use start_many to account for every required target")
        if selection.get("allowed") and target not in selection["allowed"]:
            raise FleetError("selection", "Resolved target is outside the allowed set")
        rid = secrets.token_hex(12)
        with self.store.transaction() as c:
            parent = self._parent(c)
            grant = copy.deepcopy(parent["grant"] if parent else cfg["policy"])
            # Retain the ancestor's target constraint and intersect a narrowed selection.
            targets = selection.get("allowed") or selection.get("required")
            if targets:
                old = grant.get("targets")
                grant["targets"] = [t for t in targets if not old or t in old]
                if not grant["targets"]:
                    raise FleetError("selection", "Selection broadens inherited authority")
            grant["permissions"] = [
                p for p in grant["permissions"] if p in PERMISSIONS.get(permission, set())
            ]
            stamp = time.time()
            run = dict(
                id=rid,
                generation=1,
                status="starting",
                route=native_route,
                route_id=route,
                harness=native_route["harness"],
                environment=native_route.get("environment", "host"),
                account=native_route.get("account", "default"),
                billing=billing,
                model=model,
                permission=permission,
                delegation=delegation,
                delegation_transport=native_route.get(
                    "delegation_transport", cfg["policy"].get("delegation_transport", "disabled")
                )
                if delegation == "required"
                else "disabled",
                cwd=str(Path(cwd or Path.cwd()).resolve()),
                assignment=assignment,
                parent_run=parent["id"] if parent else None,
                parent_generation=parent["generation"] if parent else None,
                root_run=parent["root_run"] if parent else rid,
                depth=parent["depth"] + 1 if parent else 1,
                caller=None,
                requesting_actor=copy.deepcopy(self._principal.get("actor")) if self._principal else None,
                grant=grant,
                created_at=stamp,
                updated_at=stamp,
                cleanup_confirmed=True,
                invoked=False,
                accepted=False,
                kept=False,
                auto_remaining=auto_resume,
                execution_token=secrets.token_hex(24),
                child_token=secrets.token_hex(32),
                invocation_count=0,
            )
            run["identity"] = f"{run['harness']}:{run['environment']}:{run['account']}:{run['model']}"
            self._validate(run, cfg, c)
            rows = self.store.all(c)
            if not self._capacity(run, rows, cfg):
                if not queue:
                    raise FleetError("capacity", "Route capacity is occupied; queuing was not requested")
                run["status"] = "queued"
            else:
                self._count_invocation(run, rows, cfg)
                run["cleanup_confirmed"] = False
            self.store.save(c, run, "admitted")
        if run["status"] == "starting":
            self._spawn(run)
        return public(run)

    def start_many(self, targets, assignment, *, selection=None, **options):
        if not isinstance(targets, list) or not targets:
            raise FleetError("selection", "Provide resolved targets")

        def valid(target):
            return isinstance(target, dict) and all(
                isinstance(target.get(key), str) and target[key].strip() for key in ("route", "model")
            )

        if selection is not None and (
            not isinstance(selection, dict)
            or any(
                selection.get(key) is not None
                and (not isinstance(selection[key], list) or not all(valid(t) for t in selection[key]))
                for key in ("allowed", "required")
            )
        ):
            raise FleetError("selection", "Selection constraints must contain resolved targets")
        selection = selection or {"required": targets}
        if len({json.dumps(t, sort_keys=True) for t in targets}) != len(targets):
            raise FleetError("selection", "Duplicate targets are not allowed")
        required = selection.get("required") or []
        missing = [t for t in required if t not in targets]
        if missing:
            raise FleetError("selection", "Every required target must be accounted for")
        # Each target gets an explicit result, including admission failures.
        result = []
        for t in targets:
            try:
                if not valid(t):
                    raise FleetError("selection", "Target must specify nonempty route and model strings")
                narrowed = {
                    "allowed": [t for t in (selection.get("allowed") or required or targets) if valid(t)]
                }
                result.append(
                    {
                        "target": t,
                        "run": self.start(
                            t["route"], assignment, model=t["model"], selection=narrowed, **options
                        ),
                    }
                )
            except FleetError as e:
                result.append({"target": t, "error": {"code": e.code, "message": str(e)}})
        return {"schema_version": 1, "results": result, "all_started": all("run" in r for r in result)}

    def _spawn(self, run, *, _from_broker=False):
        if run.get("parent_run") and not _from_broker:
            # The host supervisor claims this durable request. A tool subprocess
            # must not own the lifetime of a detached delegated supervisor.
            return
        if not _from_broker:
            from .service import status as controller_status

            if controller_status(self)["running"]:
                return
            with self.store.transaction() as c:
                current = self.store.get(run["id"], c)
                if (
                    current["generation"] != run["generation"]
                    or current["status"] != "starting"
                    or current.get("spawn_claim")
                ):
                    return
                current["spawn_claim"] = True
                self.store.save(c, current, "host_spawn_claimed")
                run = current
        env = dict(os.environ)
        for key in (
            "HARNESSFLEET_SERVICE_TOKEN",
            "HARNESSFLEET_BROKER",
            "HARNESSFLEET_BROKER_OWNER",
            "HARNESSFLEET_PARENT_RUN",
            "HARNESSFLEET_PARENT_GENERATION",
            "HARNESSFLEET_PARENT_TOKEN",
        ):
            env.pop(key, None)
        env["HARNESSFLEET_CONFIG"] = str(self.config_path)
        env["HARNESSFLEET_STATE"] = str(self.store.root)
        env["HARNESSFLEET_EXECUTION"] = f"{run['id']}:{run['generation']}:{run['execution_token']}"
        try:
            log_fd = self.store.create_log(run["id"], run["generation"])
            with os.fdopen(log_fd, "wb") as supervisor_log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "harnessfleet.worker", run["id"], str(run["generation"])],
                    env=env,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=supervisor_log,
                    close_fds=True,
                )
            with self.store.transaction() as c:
                current = self.store.get(run["id"], c)
                if current["generation"] == run["generation"] and not current.get("supervisor"):
                    current["supervisor"] = processes.receipt(process.pid)
                    current["pgid"] = process.pid
                    self.store.save(c, current, "supervisor_created")
        except OSError:
            # Popen can fail after side effects; retain capacity and require recovery.
            with self.store.transaction() as c:
                current = self.store.get(run["id"], c)
                current.update(status="uncertain", failure="supervisor_creation", updated_at=time.time())
                self.store.save(c, current, "uncertain")
            raise FleetError("supervisor", "Supervisor creation failed; invocation was not replayed")

    def status(self, rid):
        return public(self.store.get(rid))

    def list(self, offset=0, limit=50):
        if offset < 0 or not 1 <= limit <= 500:
            raise FleetError("pagination", "Invalid pagination")
        rows = self.store.all()
        return {
            "schema_version": 1,
            "runs": [public(r) for r in rows[offset : offset + limit]],
            "next_offset": offset + limit if len(rows) > offset + limit else None,
            "total": len(rows),
        }

    def output(self, rid, generation=None, partial=False):
        run = self.store.get(rid) if generation is None else self.store.generation(rid, generation)
        if not partial and not (
            run["status"] == "finished" and run.get("accepted") and run.get("cleanup_confirmed")
        ):
            raise FleetError(
                "no_final_output", "No verified final output; inspect partial evidence without replaying"
            )
        if not partial:
            try:
                receipt = json.loads(self.store.read_artifact(rid, run["generation"], "receipt.json"))
                if not isinstance(receipt, dict) or any(
                    receipt.get(k) != run.get(k)
                    for k in ("id", "generation", "invoked", "accepted", "cleanup_confirmed", "output_sha256")
                ):
                    raise ValueError()
            except (ValueError, TypeError):
                raise FleetError(
                    "receipt", "Completion receipt is invalid; consumed invocation must not be replayed"
                )
        data = self.store.read_artifact(rid, run["generation"], "partial.txt" if partial else "output.txt")
        if not partial and hashlib.sha256(data.encode()).hexdigest() != run.get("output_sha256"):
            raise FleetError(
                "receipt", "Final output receipt does not match; invocation must not be replayed"
            )
        return {
            "schema_version": 1,
            "id": rid,
            "generation": run["generation"],
            "verified": not partial,
            "text": data,
        }

    def wait(self, rid, timeout=None):
        import select

        processes.require_linux()
        watch = processes.Changes(self.store.root)
        deadline = time.monotonic() + timeout if timeout is not None else None
        try:
            while True:
                run = self.store.get(rid)
                if run["status"] in TERMINAL or run["status"] == "uncertain":
                    return public(run)
                left = deadline - time.monotonic() if deadline else 30
                if left <= 0:
                    raise FleetError("timeout", "Wait timed out; execution continues")
                peer = getattr(self, "_wait_disconnect", None)
                if peer is None:
                    watch.wait(min(left, 30))
                else:
                    ready = select.select([watch.fd, peer], [], [], min(left, 30))[0]
                    if peer in ready:
                        raise FleetError("cancelled", "Follower disconnected; child execution is unchanged")
                    if watch.fd in ready:
                        watch.wait(0)
                # Lost supervisors are reconciled without replay; no unchanged log polling.
                if run["status"] in ("running", "starting") and time.time() - run["updated_at"] > 2:
                    self.recover(rid)
        finally:
            watch.close()

    def cancel(self, rid):
        with self.store.transaction() as c:
            run = self.store.get(rid, c)
            self._resource(run, c)
            if run["status"] != "queued":
                raise FleetError("cancel", "Request was dispatched; use stop")
            run.update(status="cancelled", updated_at=time.time(), cleanup_confirmed=True)
            self.store.save(c, run, "cancelled")
        return public(run)

    def _cleanup_box(self, run):
        if not run["environment"].startswith("box:"):
            return True
        from .boxes import cleanup

        def persist():
            with self.store.transaction() as c:
                current = self.store.get(run["id"], c)
                if current["generation"] != run["generation"]:
                    raise FleetError("generation", "Box generation is obsolete")
                current["box_receipt"] = run["box_receipt"]
                self.store.save(c, current, "container_receipt")

        try:
            return cleanup(run, persist)
        except FleetError:
            return False

    def stop(self, rid):
        # Fence parent admissions before taking the descendant snapshot. Both
        # start() and stop() serialize through this transaction.
        with self.store.transaction() as c:
            run = self.store.get(rid, c)
            self._resource(run, c)
            if run["status"] == "queued":
                run.update(status="cancelled", updated_at=time.time())
                self.store.save(c, run, "cancelled")
                return public(run)
            already_clean = run["status"] in TERMINAL and run.get("cleanup_confirmed")
            if not already_clean:
                run.update(status="stopping", updated_at=time.time())
                self.store.save(c, run, "stop_requested")
            children = [
                r
                for r in self.store.all(c)
                if r.get("parent_run") == rid
                and (r["status"] not in TERMINAL or not r.get("cleanup_confirmed"))
            ]
        for child in children:
            self.stop(child["id"])
        if already_clean:
            return public(run)
        remote_clean = self._cleanup_box(run)
        clean = processes.terminate(run, include_supervisor=True) and remote_clean
        with self.store.transaction() as c:
            current = self.store.get(rid, c)
            if current["generation"] != run["generation"]:
                raise FleetError("generation", "Generation changed during stop")
            current.update(
                status="stopped" if clean else "uncertain",
                cleanup_confirmed=clean,
                accepted=False,
                updated_at=time.time(),
            )
            self.store.save(c, current, "stopped" if clean else "uncertain")
        return public(current)

    def recover(self, rid=None):
        result = []
        for run in self.store.all():
            if rid and run["id"] != rid:
                continue
            if run["status"] not in ACTIVE or processes.alive(run.get("supervisor")):
                continue
            # A just-admitted controller may not yet have registered its supervisor.
            if not run.get("supervisor") and time.time() - run["updated_at"] < 5:
                continue
            if (
                run["status"] == "starting"
                and not run.get("parent_run")
                and not run.get("supervisor")
                and not run.get("spawn_claim")
                and not run.get("invoked")
            ):
                from .service import status as controller_status

                if controller_status(self)["running"]:
                    continue
            if not run.get("supervisor") and run.get("parent_run") and not run.get("spawn_claim"):
                parent = self.store.get(run["parent_run"])
                if parent["generation"] == run["parent_generation"] and processes.alive(
                    parent.get("supervisor")
                ):
                    continue
            remote_clean = self._cleanup_box(run)
            clean = processes.terminate(run, include_supervisor=True) and remote_clean
            with self.store.transaction() as c:
                current = self.store.get(run["id"], c)
                if current["generation"] != run["generation"] or current["status"] not in ACTIVE:
                    continue
                # No recorded native process after a lost launch acknowledgement leaves
                # the invocation ambiguous, even if the local census is now empty.
                proven = clean and bool(current.get("native_process"))
                current.update(
                    status="failed" if proven else "uncertain",
                    failure="controller_lost",
                    accepted=False,
                    cleanup_confirmed=proven,
                    updated_at=time.time(),
                )
                self.store.save(c, current, "reconciled")
                result.append(public(current))
        return {"schema_version": 1, "runs": result}

    def dispatch(self):
        cfg = self.configuration()
        launch = []
        rejected = []
        with self.store.transaction() as c:
            rows = self.store.all(c)
            for run in rows:
                if run["status"] != "queued":
                    continue
                try:
                    self._validate(run, cfg, c, dispatch=True)
                    if run.get("parent_run"):
                        p = self.store.get(run["parent_run"], c)
                        if p["status"] != "running" or p["generation"] != run["parent_generation"]:
                            raise FleetError("parent", "Queued parent is no longer active")
                    if not self._capacity(run, rows, cfg):
                        continue
                    self._count_invocation(run, rows, cfg)
                    run.update(status="starting", cleanup_confirmed=False, updated_at=time.time())
                    self.store.save(c, run, "dispatched")
                    launch.append(run)
                except FleetError as e:
                    run.update(
                        status="rejected", failure=e.code, cleanup_confirmed=True, updated_at=time.time()
                    )
                    self.store.save(c, run, "rejected")
                    rejected.append(public(run))
        for run in launch:
            self._spawn(run)
        return {"schema_version": 1, "dispatched": [public(r) for r in launch], "rejected": rejected}

    def resume(self, rid, assignment, *, expected_generation=None, _automatic=False):
        processes.require_linux()
        cfg = self.configuration()
        with self.store.transaction() as c:
            run = self.store.get(rid, c)
            parent = self._parent(c)
            if parent and (run.get("parent_run"), run.get("parent_generation")) != (
                parent["id"],
                parent["generation"],
            ):
                raise FleetError("parent", "An agent can continue only its own delegated child")
            if expected_generation is not None and run["generation"] != expected_generation:
                raise FleetError("generation", "Continuation generation is obsolete")
            if (
                run["status"] not in ("finished", "quota")
                or not run.get("cleanup_confirmed")
                or not run.get("session_id")
            ):
                raise FleetError(
                    "continuation", "Continuation requires a verified terminal session and cleanup"
                )
            if run.get("parent_run"):
                owner = self.store.get(run["parent_run"], c)
                if (
                    owner["status"] != "running"
                    or owner["generation"] != run["parent_generation"]
                    or not processes.alive(owner.get("supervisor"))
                ):
                    raise FleetError(
                        "parent", "Managed child continuation requires its active host parent generation"
                    )
            if not assignment.strip() or len(assignment) > 65536:
                raise FleetError("assignment", "Invalid continuation assignment")
            if _automatic:
                a = self.adapters[run["harness"]]
                if (
                    run["status"] != "quota"
                    or run.get("auto_remaining", 0) < 1
                    or not a.capabilities.quota_recovery
                ):
                    raise FleetError("quota_recovery", "Automatic continuation is not authorized")
                if not callable(getattr(a, "quota_ready", None)) or a.quota_ready(dict(run)) is not True:
                    raise FleetError("quota_recovery", "Exact-account quota readiness is unconfirmed")
            run = copy.deepcopy(run)
            if _automatic:
                run["auto_remaining"] -= 1
            run.update(
                generation=run["generation"] + 1,
                resume_session_id=run["session_id"],
                assignment=assignment,
                status="starting",
                accepted=False,
                invoked=False,
                cleanup_confirmed=False,
                execution_token=secrets.token_hex(24),
                child_token=secrets.token_hex(32),
                supervisor=None,
                native_process=None,
                bridge_process=None,
                bridge_instance=None,
                owned_processes=[],
                requesting_actor=copy.deepcopy(self._principal.get("actor")) if self._principal else None,
                spawn_claim=False,
                box_receipt=None,
                output_sha256=None,
                failure=None,
                reset_at=None,
                session_id=None,
                pgid=None,
                updated_at=time.time(),
            )
            self._validate(run, cfg, c, dispatch=True)
            rows = self.store.all(c)
            if not self._capacity(run, rows, cfg):
                raise FleetError("capacity", "Route capacity is occupied")
            self._count_invocation(run, rows, cfg)
            self.store.save(c, run, "continuation_admitted")
        self._spawn(run)
        return public(run)

    def keep(self, rid, value=True):
        with self.store.transaction() as c:
            run = self.store.get(rid, c)
            run["kept"] = bool(value)
            self.store.save(c, run, "retention_changed")
        return public(run)

    def prune(self):
        cfg = self.configuration().get("retention", {})
        deleted = []
        now = time.time()
        for run in self.store.all():
            if run["kept"] or run["status"] not in TERMINAL or not run.get("cleanup_confirmed"):
                continue
            for gen in range(1, run["generation"] + 1):
                g = self.store.generation(run["id"], gen)
                if not g.get("cleanup_confirmed"):
                    continue
                age = (now - g["updated_at"]) / 86400
                names = []
                if age > cfg.get("events_days", 3):
                    names += ["events.jsonl", "stderr.txt", "supervisor.log"]
                    with self.store.transaction() as c:
                        c.execute(
                            "DELETE FROM events WHERE run_id=? AND generation=? AND kind='progress'",
                            (run["id"], gen),
                        )
                if age > cfg.get("output_days", 14):
                    names += ["output.txt", "partial.txt"]
                for name in names:
                    path = self.store.root / run["id"] / str(gen) / name
                    if (
                        path.is_file()
                        and not path.is_symlink()
                        and not path.parent.is_symlink()
                        and not path.parent.parent.is_symlink()
                    ):
                        path.unlink()
                        deleted.append(f"{run['id']}/{gen}/{name}")
        return {"schema_version": 1, "deleted": deleted}
