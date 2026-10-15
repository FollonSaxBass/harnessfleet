"""Optional bounded snapshots; no private workspace assumptions."""

import os
import subprocess
from pathlib import Path


def inspect(manager, kind):
    cfg = manager.configuration()
    if kind == "projects":
        result = []
        for p in cfg.get("projects", []):
            row = {"name": p["name"], "available": False}
            try:
                completed = subprocess.run(
                    ["git", "-C", str(Path(p["path"]).expanduser()), "status", "--porcelain", "--branch"],
                    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if completed.returncode == 0:
                    lines = completed.stdout.splitlines()
                    row.update(available=True, branch=lines[0][3:] if lines else None, dirty=len(lines) > 1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            result.append(row)
        return {"schema_version": 1, "projects": result}
    source = cfg.get("connectors", {}).get(kind)
    if not source or not source.get("enabled"):
        return {"schema_version": 1, "kind": kind, "availability": "disabled", "items": []}
    from .graph import load_source, valid_native_row
    from .worker import redact

    info, payload = load_source({"id": kind, "harness": "connector", "path": source["path"]})
    items, issues = [], []
    limits = {"id": 512, "parent": 512, "label": 200, "kind": 80, "status": 80, "completeness": 80}
    for index, row in enumerate(payload.get("nodes", [])):
        if not valid_native_row(row):
            issues.append({"code": "connector_record", "index": index})
            continue
        items.append({key: redact(row[key])[:limit] for key, limit in limits.items() if key in row})
    return {
        "schema_version": 1,
        "kind": kind,
        "availability": info["availability"],
        "completeness": "partial" if issues else info["completeness"],
        "items": items,
        "issues": issues,
    }
