from harnessfleet import RunManager
from harnessfleet.service import controller_once

PLUGIN = """
from harnessfleet.adapters import Codex
from harnessfleet.contracts import Capabilities
class Adapter(Codex):
    id = "synthetic-plugin"
    capabilities = Capabilities(continuation=True, quota_recovery=True,
                                permissions=("read-only",))
    def quota_ready(self, run):
        return True
"""


def test_installed_plugin_and_finite_quota_continuation(instance, monkeypatch):
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
    monkeypatch.setenv("PYTHONPATH", str(directory))
    route = {**instance.config["routes"][0], "id": "plugin", "harness": "synthetic-plugin"}
    instance.config["routes"].append(route)
    instance.config["policy"]["routes"].append("plugin")
    instance.save()
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
