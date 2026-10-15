"""Explicit integration without overwriting foreign files, links or executables."""

import os
import shutil
import sys
from importlib.resources import files
from pathlib import Path
from .contracts import FleetError

BINDINGS = {
    "codex_cli": ".agents/skills",
    "claude_code": ".claude/skills",
    "cursor_agent": ".cursor/skills",
    "agy_cli": ".gemini/skills",
    "dsh": ".dsh/skills",
    "codex_app": ".agents/skills",
    "agy_app": ".gemini/skills",
}


def install_skill(harness, directory):
    if harness not in BINDINGS:
        raise FleetError("integration", "Unknown harness binding")
    base = Path(directory).expanduser().absolute()
    if base.is_symlink():
        raise FleetError("integration", "Foreign directory links are preserved")
    base.mkdir(parents=True, exist_ok=True)
    dest = base / "harnessfleet"
    if dest.exists() or dest.is_symlink():
        raise FleetError("integration", "Existing skill binding was preserved")
    dest.mkdir()
    try:
        src = files("harnessfleet").joinpath("skill", "SKILL.md").read_text()
        (dest / "SKILL.md").write_text(src)
    except BaseException:
        shutil.rmtree(dest)
        raise
    return {
        "schema_version": 1,
        "harness": harness,
        "installed": str(dest),
        "binding_hint": BINDINGS[harness],
    }


def compatibility(directory):
    base = Path(directory).expanduser().absolute()
    if base.is_symlink():
        raise FleetError("integration", "Foreign directory links are preserved")
    base.mkdir(parents=True, exist_ok=True)
    path = base / "agent-run"
    # Also protect existing commands anywhere on PATH, not only at destination.
    if shutil.which("agent-run") or path.exists() or path.is_symlink():
        raise FleetError(
            "coexistence", "An agent-run command already exists; compatibility activation refused"
        )
    import shlex

    content = "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -m harnessfleet run "$@"\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
    with os.fdopen(fd, "w") as f:
        f.write(content)
        os.fchmod(f.fileno(), 0o755)
    return {"schema_version": 1, "installed": str(path)}
