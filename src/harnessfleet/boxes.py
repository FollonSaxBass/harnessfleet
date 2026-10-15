"""Experimental Linux amd64 Boxes, bound to a local daemon and ownership receipts."""

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from .contracts import FleetError

LABEL = "ai.harnessfleet.execution"
CID = re.compile(r"[a-f0-9]{64}\Z")


def validate(run):
    box = run["route"].get("box", {})
    if platform.machine() not in ("x86_64", "amd64"):
        raise FleetError("platform", "Box arm64 is not qualified")
    if not box.get("daemon", "").startswith("unix:///"):
        raise FleetError("daemon", "Boxes require an explicit local Unix Docker daemon")
    if not box.get("image") or not box.get("account_home") or run["route"].get("environment_vars"):
        raise FleetError(
            "box", "Configure a dedicated Box image/account; host native homes are not inherited"
        )
    account = Path(box["account_home"]).expanduser().absolute()
    forbidden = {Path.home() / ".codex", Path.home() / ".claude", Path.home(), Path("/")}
    if account in forbidden or account.is_symlink() or not account.is_dir():
        raise FleetError("box", "A dedicated regular account directory is required")
    target = box.get("native_home", "/account")
    if not target.startswith("/") or target in ("/", "/root", "/home"):
        raise FleetError("box", "Configure an explicit native home mount target")
    mounts = box.get("mounts", [])
    mapped = False
    for mount in mounts:
        source = Path(mount["source"]).expanduser().absolute()
        if source.is_symlink() or not source.is_dir() or source in (Path("/"), Path.home()):
            raise FleetError("mount", "Box mounts must name explicit regular project directories")
        if (
            mount.get("mode") not in ("ro", "rw")
            or not mount["target"].startswith("/")
            or mount["target"] == "/"
        ):
            raise FleetError("mount", "Box mounts need explicit targets and ro/rw modes")
        if "," in str(source) + mount["target"]:
            raise FleetError("mount", "Mount paths containing commas are unsupported")
        if Path(run["cwd"]).is_relative_to(source):
            mapped = True
    if not mapped:
        raise FleetError("mount", "Working directory must be covered by an explicit Box mount")
    if run["harness"] not in ("codex_cli", "claude_code"):
        raise FleetError("box", "Only Codex and Claude Box protocols are implemented")
    return box


def diagnose(route):
    box = route.get("box", {})
    try:
        validate({"route": route, "harness": route["harness"], "cwd": box["mounts"][0]["source"]})
        daemon(box)
        return {
            "available": True,
            "authentication": "unknown",
            "guidance": "Authenticate the dedicated account inside the configured image; native qualification remains pending",
        }
    except (FleetError, KeyError, IndexError):
        return {
            "available": False,
            "authentication": "unknown",
            "guidance": "Configure local Docker, a dedicated account and explicit accessible mounts",
        }


def rpc(box, args, check=True):
    executable = box.get("docker_executable", "docker")
    if not shutil.which(executable):
        raise FleetError("docker", "Docker CLI is unavailable")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
    try:
        result = subprocess.run(
            [executable, "--host", box["daemon"], *args], env=env, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        raise FleetError("docker", "Docker operation has uncertain outcome")
    if check and result.returncode:
        raise FleetError("docker", "Docker operation failed; ownership must be reconciled")
    return result


def daemon(box):
    try:
        data = json.loads(rpc(box, ["info", "--format", "{{json .}}"]).stdout)
        identity = {k: data[k] for k in ("ID", "OSType", "Architecture")}
        if (
            identity["OSType"] != "linux"
            or identity["Architecture"] not in ("x86_64", "amd64")
            or not identity["ID"]
        ):
            raise ValueError()
        # Docker Desktop needs independent filesystem/ownership qualification.
        if "docker desktop" in str(data.get("OperatingSystem", "")).casefold():
            raise ValueError()
        return {"endpoint": box["daemon"], **identity}
    except (ValueError, KeyError, TypeError):
        raise FleetError("daemon", "Only a qualified local Linux amd64 daemon is accepted")


def verify(box, receipt):
    if daemon(box) != receipt["daemon"]:
        raise FleetError(
            "daemon_changed", "Recorded Docker daemon changed; no container effect was attempted"
        )


def inspect(box, receipt):
    verify(box, receipt)
    identity = receipt.get("container_id") or receipt["name"]
    response = rpc(box, ["container", "inspect", identity], check=False)
    if response.returncode:
        # A failed inspect is not proof of absence or ownership release.
        return None
    try:
        rows = json.loads(response.stdout)
        row = rows[0]
        if not CID.fullmatch(row["Id"]) or row["Config"]["Labels"].get(LABEL) != receipt["owner"]:
            raise ValueError()
        if receipt.get("container_id") and row["Id"] != receipt["container_id"]:
            raise ValueError()
        if row["Name"].lstrip("/") != receipt["name"]:
            raise ValueError()
        return row
    except (ValueError, TypeError, KeyError, IndexError):
        raise FleetError("container_ownership", "Container identity/ownership receipt does not match")


def prepare(run, command, persist, effect):
    box = validate(run)
    receipt = {
        "daemon": daemon(box),
        "name": f"harnessfleet-{run['id']}-{run['generation']}",
        "owner": run["execution_token"],
        "container_id": None,
        "creation": "pending",
        "released": False,
    }
    run["box_receipt"] = receipt
    persist()
    native_home = box.get("native_home", "/account")
    a = [
        "container",
        "create",
        "--name",
        receipt["name"],
        "--label",
        LABEL + "=" + receipt["owner"],
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        "HARNESSFLEET_BOX=1",
        "--env",
        "AGENT_IDENTITY=" + run["identity"],
        "--mount",
        f"type=bind,source={Path(box['account_home']).expanduser().absolute()},target={native_home}",
    ]
    a += [
        "--env",
        ("CODEX_HOME" if run["harness"] == "codex_cli" else "CLAUDE_CONFIG_DIR") + "=" + native_home,
    ]
    container_cwd = None
    for mount in box["mounts"]:
        source = Path(mount["source"]).expanduser().absolute()
        suffix = ",readonly" if mount["mode"] == "ro" else ""
        a += ["--mount", f"type=bind,source={source},target={mount['target']}{suffix}"]
        if Path(run["cwd"]).is_relative_to(source):
            container_cwd = str(Path(mount["target"]) / Path(run["cwd"]).relative_to(source))
    a += ["--workdir", container_cwd, box["image"], *command]
    verify(box, receipt)
    response = effect(lambda: rpc(box, a))
    cid = response.stdout.strip()
    if not CID.fullmatch(cid):
        raise FleetError("container_ack", "Container creation acknowledgement is missing; do not replay")
    receipt.update(container_id=cid, creation="acknowledged")
    persist()
    row = inspect(box, receipt)
    if not row:
        raise FleetError("container_ack", "Created container ownership could not be verified")
    verify(box, receipt)
    return [
        box.get("docker_executable", "docker"),
        "--host",
        box["daemon"],
        "container",
        "start",
        "--attach",
        cid,
    ]


def cleanup(run, persist):
    box = run["route"]["box"]
    receipt = run.get("box_receipt")
    if not receipt:
        return not run.get("invoked")
    if receipt.get("released"):
        return True
    row = inspect(box, receipt)
    if row is None:
        return False
    # The exact owner label plus recorded daemon/name can reconcile a lost create ack.
    receipt["container_id"] = row["Id"]
    persist()
    if row["State"]["Running"]:
        verify(box, receipt)
        rpc(box, ["container", "stop", "--time", "1", row["Id"]])
        row = inspect(box, receipt)
        if not row or row["State"]["Running"]:
            return False
    verify(box, receipt)
    response = rpc(box, ["container", "rm", row["Id"]])
    if response.stdout.strip() != row["Id"]:
        return False
    receipt.update(released=True, creation="acknowledged")
    persist()
    return True
