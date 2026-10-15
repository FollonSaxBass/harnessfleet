import json
import os
import subprocess
from pathlib import Path
import pytest
from harnessfleet import FleetError
from harnessfleet.integration import compatibility, install_skill
from harnessfleet.config import initialize
from harnessfleet import processes


def test_doctor_has_no_model_invocations(instance):
    result = json.loads(instance.cli("doctor").stdout)
    assert result["models_invoked"] is False
    assert len(result["routes"]) == 3
    assert not instance.calls()


def test_missing_prerequisites_are_structured(instance):
    instance.config["routes"][0]["executable"] = "harnessfleet-nonexistent-native"
    instance.save()
    result = json.loads(instance.cli("doctor").stdout)
    assert result["routes"][0]["readiness"]["available"] is False
    assert "Install codex" in result["routes"][0]["readiness"]["guidance"]


def test_init_and_skill_install_preserve_foreign_configuration(instance):
    p = instance.path / "new-config.json"
    initialize(p)
    with pytest.raises(FleetError):
        initialize(p)
    d = instance.path / "skills"
    d.mkdir()
    (d / "foreign").mkdir()
    result = install_skill("claude_code", d)
    assert (d / "foreign").is_dir() and Path(result["installed"], "SKILL.md").is_file()
    with pytest.raises(FleetError):
        install_skill("claude_code", d)


def test_skill_directory_symlinks_are_preserved(instance):
    foreign = instance.path / "foreign"
    foreign.mkdir()
    d = instance.path / "skills"
    d.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(FleetError):
        install_skill("codex_cli", d)
    assert list(foreign.iterdir()) == []


def test_compatibility_never_overwrites_existing_commands(instance, monkeypatch):
    binary = instance.path / "bin"
    binary.mkdir()
    command = binary / "agent-run"
    command.write_text("foreign")
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary))
    with pytest.raises(FleetError):
        compatibility(instance.path / "new-bin")
    assert command.read_text() == "foreign"


def test_offline_demo_outside_git(instance):
    result = json.loads(instance.cli("demo", "--directory", str(instance.path / "demo")).stdout)
    assert result["synthetic"] and not instance.calls()
    assert Path(result["config"]).exists()
    cfg = json.loads(Path(result["config"]).read_text())
    assert cfg["routes"] == [] and cfg["policy"]["routes"] == []


def test_scrubbed_marker_never_proves_cleanup(instance):
    sentinel = subprocess.Popen(["/usr/bin/env", "-i", "/bin/sleep", "30"], start_new_session=True)
    try:
        record = {"id": "a" * 24, "generation": 1, "execution_token": "synthetic", "pgid": sentinel.pid}
        assert processes.terminate(record) is False
        assert sentinel.poll() is None
    finally:
        sentinel.terminate()
        sentinel.wait()


def test_retention_protects_kept_and_uncertain_records(instance):
    run = instance.start()
    instance.finish(run)
    with instance.m.store.transaction() as c:
        r = instance.m.store.get(run["id"], c)
        r["updated_at"] = 0
        instance.m.store.save(c, r, "synthetic_old_record")
    instance.m.keep(run["id"])
    assert instance.m.prune()["deleted"] == []
    instance.m.keep(run["id"], False)
    deleted = instance.m.prune()["deleted"]
    assert any(p.endswith("output.txt") for p in deleted)
    with pytest.raises(FleetError):
        instance.m.output(run["id"])
    assert len(instance.calls()) == 1


def test_host_controller_launches_roots_independently(instance):
    import select
    from harnessfleet import service

    started = service.start(instance.m)
    assert started["running"]
    try:
        run = instance.start()
        assert instance.finish(run)["status"] == "finished"
        assert instance.m.output(run["id"])["verified"]
        assert len(instance.calls()) == 1
        pid = started["process"]["pid"]
        fd = processes.pidfd_open(pid)
        try:
            assert service.stop(instance.m)["stop_requested"]
            assert select.select([fd], [], [], 5)[0]
        finally:
            os.close(fd)
        assert not service.status(instance.m)["running"]
    finally:
        service.stop(instance.m)
