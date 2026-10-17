import json
import os
import sys
from pathlib import Path

import pytest
import harnessfleet
from harnessfleet import FleetError, RunManager
from harnessfleet.service import controller_once

PLUGIN = """
import json, os
from pathlib import Path
from harnessfleet.adapters import Codex
from harnessfleet.contracts import Capabilities
class Adapter(Codex):
    id = "synthetic-plugin"
    capabilities = Capabilities(continuation=True, quota_recovery=True,
                                permissions=("read-only",))
    def quota_ready(self, run):
        path = Path(os.environ["HF_TEST_PLUGIN_READY_FILE"])
        observation = json.loads(path.read_text())
        if isinstance(observation, list):
            ready, following = observation
            path.write_text(json.dumps(following))
            return ready
        return observation
"""


@pytest.fixture
def installed_plugin(instance, monkeypatch):
    directory = instance.path / "plugins"
    directory.mkdir()
    (directory / "synthetic_adapter.py").write_text(PLUGIN)
    metadata = directory / "synthetic_harnessfleet-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: synthetic-harnessfleet\nVersion: 1.0\n")
    (metadata / "entry_points.txt").write_text(
        "[harnessfleet.adapters]\nsynthetic = synthetic_adapter:Adapter\n"
    )
    monkeypatch.syspath_prepend(str(directory))
    package_root = Path(harnessfleet.__file__).resolve().parent.parent
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(directory), str(package_root))))
    monkeypatch.delitem(sys.modules, "synthetic_adapter", raising=False)
    route = {**instance.config["routes"][0], "id": "plugin", "harness": "synthetic-plugin"}
    instance.config["routes"].append(route)
    instance.config["policy"]["routes"].append("plugin")
    instance.save()
    readiness = instance.path / "plugin_quota_ready.json"
    readiness.write_text("true")
    monkeypatch.setenv("HF_TEST_PLUGIN_READY_FILE", str(readiness))
    return readiness


def test_installed_plugin_and_finite_quota_continuation(instance, installed_plugin):
    manager = RunManager()
    run = manager.start("plugin", "mode:quota", auto_resume=1)
    assert manager.wait(run["id"], 10)["status"] == "quota"
    assert controller_once(manager)["continued"][0]["generation"] == 2
    final = manager.wait(run["id"], 10)
    assert final["status"] == "finished" and final["auto_remaining"] == 0
    assert manager.output(run["id"])["verified"]
    assert len(instance.calls()) == 2
    assert controller_once(manager)["continued"] == []
    assert manager.store.generation(run["id"], 1)["status"] == "quota"


@pytest.mark.parametrize(
    "observation",
    [False, None, 1, [True, False]],
    ids=["false", "unknown", "integer-one", "changed-before-admission"],
)
def test_unconfirmed_quota_preserves_budget_generation_and_route_capacity(
    instance, installed_plugin, observation
):
    installed_plugin.write_text(json.dumps(observation))
    manager = RunManager()
    run = manager.start("plugin", "mode:quota", auto_resume=1)
    initial = manager.wait(run["id"], 10)
    assert initial["status"] == "quota" and initial["cleanup_confirmed"]
    assert len(instance.calls()) == 1

    assert controller_once(manager)["continued"] == []
    assert json.loads(instance.cli("service", "once").stdout)["continued"] == []
    current = json.loads(instance.cli("run", "status", run["id"]).stdout)
    assert (current["status"], current["generation"], current["auto_remaining"], current["accepted"]) == (
        "quota",
        1,
        1,
        False,
    )
    assert len(instance.calls()) == 1
    with pytest.raises(FleetError) as unavailable:
        manager.output(run["id"], generation=1)
    assert unavailable.value.code == "no_final_output"

    # A distinct authorized launch on the same single-capacity Route proves no leaked claim.
    probe = manager.start("plugin", "mode:leaf")
    assert manager.wait(probe["id"], 10)["status"] == "finished"
    assert manager.status(run["id"])["auto_remaining"] == 1

    installed_plugin.write_text("true")
    continued = json.loads(instance.cli("service", "once").stdout)["continued"]
    assert len(continued) == 1 and (continued[0]["id"], continued[0]["generation"]) == (run["id"], 2)
    final = manager.wait(run["id"], 10)
    assert (final["status"], final["generation"], final["auto_remaining"], final["accepted"]) == (
        "finished",
        2,
        0,
        True,
    )
    output = json.loads(instance.cli("run", "output", run["id"]).stdout)
    assert output["verified"] and output["generation"] == 2 and output["text"] == "synthetic final"
    assert len([call for call in instance.calls() if call["run"] == run["id"]]) == 2
    assert len(instance.calls()) == 3
    assert controller_once(manager)["continued"] == []
    old = manager.store.generation(run["id"], 1)
    assert (old["status"], old["generation"], old["auto_remaining"], old["accepted"]) == (
        "quota",
        1,
        1,
        False,
    )
    assert manager.store.generation(run["id"], 2)["accepted"] is True


def test_built_in_native_routes_refuse_quota_recovery(instance):
    manager = RunManager()
    for name in ("codex_cli", "claude_code", "cursor_agent", "dsh", "codex_app", "agy_cli", "agy_app"):
        assert manager.adapters[name].capabilities.quota_recovery is False
    with pytest.raises(FleetError) as refused:
        manager.start("codex-local", "mode:quota", auto_resume=1)
    assert refused.value.code == "quota_recovery"
    result = instance.cli(
        "run", "start", "--route", "codex-local", "--auto-resume", "1", "mode:quota", ok=False
    )
    assert result.returncode == 2 and json.loads(result.stderr)["error"]["code"] == "quota_recovery"
    assert not instance.calls() and not manager.list()["runs"]
