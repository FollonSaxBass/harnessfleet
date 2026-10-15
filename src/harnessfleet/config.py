"""User-owned configuration; initialization never discovers accounts or sessions."""

import json
import os
from pathlib import Path
from .contracts import FleetError

HARNESS_IDS = ("codex_cli", "codex_app", "claude_code", "agy_cli", "agy_app", "cursor_agent", "dsh")


def locations():
    config = Path(
        os.environ.get(
            "HARNESSFLEET_CONFIG",
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "harnessfleet/config.json",
        )
    )
    state = Path(
        os.environ.get(
            "HARNESSFLEET_STATE",
            Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "harnessfleet",
        )
    )
    return config.absolute(), state.absolute()


def defaults():
    return {
        "schema_version": 1,
        "routes": [],
        "policy": {
            "routes": [],
            "billing": ["subscription"],
            "permissions": ["read-only"],
            "max_depth": 2,
            "max_parallel": 2,
            "max_invocations": 20,
            "cwd_roots": [],
            "experimental": False,
        },
        "sources": [],
        "projects": [],
        "connectors": {},
        "notifications": {},
        "retention": {"events_days": 3, "output_days": 14},
        "dashboard": {"port": 8421},
    }


def initialize(path=None):
    path = Path(path or locations()[0])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(defaults(), f, indent=2)
            f.write("\n")
    except FileExistsError:
        raise FleetError("exists", "Configuration exists; it was preserved")
    return str(path)


def load(path=None):
    path = Path(path or locations()[0])
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError()
        with path.open() as f:
            data = json.load(f)
        if data["schema_version"] != 1:
            raise ValueError()
        routes = data["routes"]
        ids = [r["id"] for r in routes]
        if len(ids) != len(set(ids)):
            raise ValueError()
        for r in routes:
            if r["billing"] not in ("subscription", "metered") or not r["id"]:
                raise ValueError()
            if type(r.get("concurrency", 1)) is not int or r.get("concurrency", 1) < 1:
                raise ValueError()
        p = data["policy"]
        for key in ("max_depth", "max_parallel", "max_invocations"):
            if type(p[key]) is not int or p[key] < 1:
                raise ValueError()
        if p["max_depth"] > 2:
            raise ValueError()
        return data
    except FileNotFoundError:
        raise FleetError(
            "configuration", "Run harnessfleet init, then explicitly configure Routes and policy"
        )
    except (ValueError, KeyError, TypeError):
        raise FleetError("configuration", "Invalid version 1 configuration")
