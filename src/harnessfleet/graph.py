"""Versioned graph projection. Authority and causal lineage are separate edges."""

import hashlib
import json
import os
import stat
from pathlib import Path
from .contracts import FleetError
from .manager import public
from .worker import redact

COMPLETENESS = {"complete", "partial", "expired", "unsupported", "missing", "unreadable"}


def native_key(source, identifier, binding=None):
    namespace = json.dumps(
        [source["id"], source["harness"], source["environment"], source["account"], identifier, binding],
        sort_keys=True,
        separators=(",", ":"),
    )
    return "native:" + hashlib.sha256(namespace.encode()).hexdigest()


def run_key(rid, generation):
    return f"run:{rid}:{generation}"


def load_source(source):
    if source.get("format") == "cursor-transcripts-v1":
        from .cursor_source import load

        return load(source)
    info = {
        "id": source["id"],
        "harness": source["harness"],
        "availability": "available",
        "completeness": "complete",
        "capabilities": {"observation": True, "control": False, "output": True},
    }
    path = Path(source["path"]).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise ValueError()
            data = f.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError()
        payload = json.loads(data)
        if payload.get("schema_version") != 1 or not isinstance(payload.get("nodes"), list):
            raise ValueError()
        info["completeness"] = payload.get("completeness", "partial")
        if info["completeness"] not in COMPLETENESS:
            raise ValueError()
        return info, payload
    except FileNotFoundError:
        info.update(availability="missing", completeness="missing")
    except (OSError, ValueError, TypeError, AttributeError):
        info.update(availability="unreadable", completeness="unreadable")
    return info, {"nodes": []}


def valid_native_row(row):
    if (
        not isinstance(row, dict)
        or not isinstance(row.get("id"), str)
        or not row["id"]
        or len(row["id"]) > 512
    ):
        return False
    for key in (
        "kind",
        "label",
        "status",
        "completeness",
        "parent",
        "session_id",
        "managed_session_id",
        "reply",
    ):
        if key in row and not isinstance(row[key], str):
            return False
    for key in ("binding", "native_parent_binding"):
        if key in row and row[key] is not None:
            b = row[key]
            if (
                not isinstance(b, dict)
                or not isinstance(b.get("run_id"), str)
                or type(b.get("generation")) is not int
            ):
                return False
    calls = row.get("delegations", [])
    if not isinstance(calls, list):
        return False
    return all(
        isinstance(c, dict) and isinstance(c.get("run_id"), str) and type(c.get("generation")) is int
        for c in calls
    )


class Graph:
    def __init__(self, manager):
        self.manager = manager

    def snapshot(self):
        m = self.manager
        cfg = m.configuration()
        nodes = {}
        edges = []
        issues = []
        sources = []
        generations = {}
        native_callers = {}

        def edge(parent, child, kind):
            item = {"parent": parent, "child": child, "kind": kind}
            if item not in edges:
                edges.append(item)

        for run in m.store.all():
            for generation in range(1, run["generation"] + 1):
                g = m.store.generation(run["id"], generation)
                key = run_key(run["id"], generation)
                generations[(run["id"], generation)] = g
                nodes[key] = {
                    "id": key,
                    "kind": "agent_run",
                    "label": g["identity"],
                    "status": g["status"],
                    "run": public(g),
                    "completeness": "complete",
                    "output_available": g.get("accepted", False),
                }
                if g.get("parent_run"):
                    edge(run_key(g["parent_run"], g["parent_generation"]), key, "authority")
        seen_sources = set()
        configured_sources = cfg.get("sources", [])
        if not isinstance(configured_sources, list):
            issues.append({"code": "source_configuration", "source": None})
            configured_sources = []
        for source in configured_sources:
            if not isinstance(source, dict):
                issues.append({"code": "source_configuration", "source": None})
                continue
            if not source.get("enabled", False):
                continue
            required = ("id", "harness", "environment", "account", "path")
            if not all(isinstance(source.get(k), str) for k in required) or source["id"] in seen_sources:
                issues.append({"code": "source_configuration", "source": source.get("id")})
                continue
            seen_sources.add(source["id"])
            if source.get("format", "normalized-v1") not in ("normalized-v1", "cursor-transcripts-v1"):
                sources.append(
                    {
                        "id": source["id"],
                        "availability": "available",
                        "completeness": "unsupported",
                        "capabilities": {"observation": False, "control": False, "output": False},
                    }
                )
                continue
            info, payload = load_source(source)
            issues.extend({**issue, "source": source["id"]} for issue in info.pop("issues", []))
            cache_key = "source:" + native_key(source, "metadata")
            with m.store.transaction() as c:
                cached = c.execute("SELECT data FROM meta WHERE key=?", (cache_key,)).fetchone()
                if info["availability"] == "available":
                    compact = {
                        **payload,
                        "nodes": [
                            {k: v for k, v in n.items() if k != "reply"}
                            for n in payload["nodes"]
                            if isinstance(n, dict)
                        ],
                    }
                    encoded = json.dumps(compact, sort_keys=True)
                    if not cached or cached[0] != encoded:
                        c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (cache_key, encoded))
                elif cached:
                    payload = json.loads(cached[0])
                    payload["nodes"] = [{**n, "completeness": "expired"} for n in payload["nodes"]]
            sources.append(info)
            local = {}
            resolved = {}
            for index, row in enumerate(payload["nodes"]):
                if not valid_native_row(row):
                    info["completeness"] = "partial"
                    issues.append({"code": "malformed_native_record", "source": source["id"]})
                    continue
                binding = row.get("binding")
                if binding is not None:
                    try:
                        owned = generations[(binding["run_id"], binding["generation"])]
                        if owned.get("session_id") != row.get(
                            "managed_session_id", row.get("session_id", row["id"])
                        ) or any(owned.get(k) != source[k] for k in ("harness", "environment", "account")):
                            raise ValueError()
                    except (KeyError, ValueError, TypeError):
                        issues.append({"code": "invalid_generation_binding", "source": source["id"]})
                        continue
                key = native_key(source, row["id"], binding)
                occurrence = (row["id"], json.dumps(binding, sort_keys=True))
                if key in nodes:
                    issues.append({"code": "duplicate_native_identity", "id": key})
                    continue
                local[occurrence] = key
                resolved[index] = key
                completeness = row.get("completeness", info["completeness"])
                nodes[key] = {
                    "id": key,
                    "kind": row.get("kind", "session"),
                    "label": redact(str(row.get("label", row["id"])))[:200],
                    "status": str(row.get("status", "unknown"))[:80],
                    "harness": source["harness"],
                    "source": source["id"],
                    "completeness": completeness if completeness in COMPLETENESS else "partial",
                    "output_available": isinstance(row.get("reply"), str),
                    "reply": redact(row.get("reply", ""))[:65536],
                    "observed": True,
                    "control": False,
                }
                if binding:
                    edge(run_key(binding["run_id"], binding["generation"]), key, "causal")
            for index, row in enumerate(payload["nodes"]):
                if index not in resolved:
                    continue
                key = resolved[index]
                if row.get("parent"):
                    parent_binding = row.get("native_parent_binding", row.get("binding"))
                    parent = local.get((row["parent"], json.dumps(parent_binding, sort_keys=True)))
                    if parent:
                        edge(parent, key, "causal")
                    else:
                        nodes[key]["completeness"] = "partial"
                        issues.append({"code": "unknown_native_parent", "id": key})
                for call in row.get("delegations", []):
                    try:
                        g = generations[(call["run_id"], call["generation"])]
                        # Attribution must identify both the managed parent generation
                        # and the native caller's association with that generation.
                        binding = row.get("binding")
                        if call.get("parent_binding", binding) != binding:
                            raise ValueError()
                        if not binding or (g.get("parent_run"), g.get("parent_generation")) != (
                            binding["run_id"],
                            binding["generation"],
                        ):
                            raise ValueError()
                        child = run_key(call["run_id"], call["generation"])
                        if child in native_callers and native_callers[child] != key:
                            native_callers[child] = None
                            issues.append({"code": "ambiguous_caller", "id": child})
                        elif child not in native_callers:
                            native_callers[child] = key
                    except (KeyError, ValueError, TypeError):
                        issues.append({"code": "unattributed_delegation", "id": key})
        for (rid, generation), g in generations.items():
            child = run_key(rid, generation)
            caller = native_callers.get(child)
            if caller:
                edge(caller, child, "causal")
                nodes[child]["run"]["caller"] = caller
            elif g.get("parent_run"):
                # This edge states execution authority, not an invented exact native caller.
                edge(run_key(g["parent_run"], g["parent_generation"]), child, "managed_parent")
                nodes[child]["caller_status"] = "unknown"
        # Cycles stay explicit as rejected relationships. Every node remains navigable.
        display = {}
        accepted = []
        for e in sorted(edges, key=lambda e: (e["kind"] != "causal", e["parent"], e["child"])):
            if e["kind"] == "authority":
                accepted.append(e)
                continue
            stack = [e["child"]]
            visited = set()
            cycle = False
            while stack:
                n = stack.pop()
                if n == e["parent"]:
                    cycle = True
                    break
                if n in visited:
                    continue
                visited.add(n)
                stack.extend(display.get(n, []))
            if cycle:
                issues.append({"code": "cycle", "edge": e})
            else:
                display.setdefault(e["parent"], []).append(e["child"])
                accepted.append(e)
        return {"schema_version": 1, "nodes": nodes, "edges": accepted, "sources": sources, "issues": issues}

    def page(self, parent=None, offset=0, limit=50, query=""):
        if offset < 0 or not 1 <= limit <= 500:
            raise FleetError("pagination", "Invalid graph pagination")
        graph = self.snapshot()
        nodes = graph["nodes"]
        edges = [e for e in graph["edges"] if e["kind"] != "authority"]
        if parent and parent not in nodes:
            raise FleetError("node", "Graph node is absent or its source is unavailable")
        retained = set(nodes)
        if query:
            retained = {k for k, n in nodes.items() if query.casefold() in json.dumps(n).casefold()}
            # Ancestor closure: filters never detach a matching descendant.
            changed = True
            while changed:
                changed = False
                for e in edges:
                    if e["child"] in retained and e["parent"] not in retained:
                        retained.add(e["parent"])
                        changed = True
        incoming = {e["child"] for e in edges}
        candidates = sorted(
            {e["child"] for e in edges if e["parent"] == parent} if parent else set(nodes) - incoming
        )
        candidates = [k for k in candidates if k in retained]
        page = []
        for key in candidates[offset : offset + limit]:
            node = dict(nodes[key])
            node.pop("reply", None)
            children = {e["child"] for e in edges if e["parent"] == key and e["child"] in retained}
            node["available_children"] = len(children)
            node["leaf_confirmed"] = not children and node["completeness"] == "complete"
            page.append(node)
        return {
            "schema_version": 1,
            "nodes": page,
            "parent": parent,
            "total": len(candidates),
            "next_offset": offset + limit if len(candidates) > offset + limit else None,
            "sources": graph["sources"],
            "issues": graph["issues"],
        }

    def output(self, key):
        node = self.snapshot()["nodes"].get(key)
        if not node:
            raise FleetError("node", "Node is unavailable")
        if node["kind"] == "agent_run":
            run = node["run"]
            return self.manager.output(run["id"], run["generation"])
        if not node.get("output_available"):
            raise FleetError("output", "Native reply is missing, expired or unsupported")
        return {"schema_version": 1, "id": key, "verified": False, "observed": True, "text": node["reply"]}
