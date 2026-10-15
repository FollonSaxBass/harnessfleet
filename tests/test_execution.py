import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from harnessfleet import FleetError
from harnessfleet import processes


def test_installed_cli_lifecycle(instance):
    r = json.loads(
        instance.cli(
            "run", "start", "--route", "codex-local", "--model", "model-a", "bounded assignment"
        ).stdout
    )
    final = json.loads(instance.cli("run", "follow", r["id"], "--timeout", "10").stdout)
    assert final["status"] == "finished"
    result = json.loads(instance.cli("run", "output", r["id"]).stdout)
    assert result["verified"] and result["text"] == "synthetic final"
    assert len(instance.calls()) == 1
    assert "execution_token" not in json.dumps(final)
    assert "child_token" not in json.dumps(final)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"model": "missing"}, "model"),
        ({"billing": "metered"}, "billing"),
        ({"permission": "unrestricted"}, "permission"),
        ({"auto_resume": 1}, "quota_recovery"),
        ({"selection": {"allowed": [{"route": "claude-local", "model": "model-b"}]}}, "selection"),
    ],
)
def test_refuses_before_invocation(instance, change, code):
    with pytest.raises(FleetError) as e:
        instance.start(**change)
    assert e.value.code == code
    assert not instance.calls()
    assert instance.m.list()["total"] == 0


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("routes", [], "policy"),
        ("experimental", False, "experimental"),
        ("cwd_roots", [], "cwd"),
        ("expires_at", 0, "grant"),
    ],
)
def test_policy_denial_has_no_native_effect(instance, field, value, code):
    instance.config["policy"][field] = value
    instance.save()
    with pytest.raises(FleetError) as e:
        instance.start()
    assert e.value.code == code and not instance.calls()


def test_metered_needs_selection_and_grant(instance):
    with pytest.raises(FleetError):
        instance.m.start("metered", "assignment", billing="metered")
    instance.config["policy"]["billing"].append("metered")
    instance.save()
    r = instance.m.start("metered", "assignment", billing="metered")
    assert instance.finish(r)["status"] == "finished"


def test_required_set_accounts_for_unavailable_member(instance):
    targets = [{"route": "codex-local", "model": "model-a"}, {"route": "missing", "model": "missing"}]
    response = json.loads(
        instance.cli("run", "batch", "assignment", "--targets", json.dumps(targets), ok=False).stdout
    )
    assert response["all_started"] is False
    assert "run" in response["results"][0] and response["results"][1]["error"]["code"] == "target"
    instance.finish(response["results"][0]["run"])


def test_required_selection_cannot_omit_members(instance):
    targets = [{"route": "codex-local", "model": "model-a"}]
    with pytest.raises(FleetError):
        instance.m.start_many(
            targets, "a", selection={"required": targets + [{"route": "other", "model": "x"}]}
        )
    assert not instance.calls()


@pytest.mark.parametrize(
    "invalid",
    [{"route": "codex-local"}, None, "codex-local", [], {"model": "a"}, {"route": "", "model": "a"}],
)
@pytest.mark.parametrize("invalid_first", [False, True])
def test_batch_accounts_for_malformed_member_without_losing_admitted_run(instance, invalid, invalid_first):
    targets = [{"route": "codex-local", "model": "model-a"}, invalid]
    if invalid_first:
        targets.reverse()
    result = instance.cli("run", "batch", "assignment", "--targets", json.dumps(targets), ok=False)
    assert result.returncode == 1, result.stderr
    response = json.loads(result.stdout)
    assert not response["all_started"]
    assert response["results"][0 if invalid_first else 1]["error"]["code"] == "selection"
    run = response["results"][1 if invalid_first else 0]["run"]
    assert instance.finish(run)["status"] == "finished"
    assert [call["run"] for call in instance.calls()] == [run["id"]]


@pytest.mark.parametrize(
    "selection",
    [[], {"allowed": "codex-local"}, {"required": [None]}, {"allowed": [{"route": "codex-local"}]}],
)
def test_malformed_batch_constraints_are_rejected_before_admission(instance, selection):
    targets = [{"route": "codex-local", "model": "model-a"}]
    result = instance.cli(
        "run",
        "batch",
        "assignment",
        "--targets",
        json.dumps(targets),
        "--selection",
        json.dumps(selection),
        ok=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]["code"] == "selection"
    assert instance.m.list()["total"] == 0 and not instance.calls()


@pytest.mark.parametrize(
    "selection", [{"allowed": None}, {"required": None}, {"allowed": None, "required": None}]
)
def test_nullable_batch_constraints_preserve_absent_constraint_behavior(instance, selection):
    targets = [{"route": "codex-local", "model": "model-a"}]
    response = json.loads(
        instance.cli(
            "run",
            "batch",
            "assignment",
            "--targets",
            json.dumps(targets),
            "--selection",
            json.dumps(selection),
        ).stdout
    )
    assert response["all_started"]
    run = response["results"][0]["run"]
    assert instance.finish(run)["status"] == "finished"
    assert [call["run"] for call in instance.calls()] == [run["id"]]


def test_capacity_queue_and_cancellation(instance):
    r = instance.start("mode:slow")
    with pytest.raises(FleetError) as e:
        instance.start()
    assert e.value.code == "capacity"
    q = instance.start(queue=True)
    assert q["status"] == "queued" and not q["invoked"]
    assert instance.m.cancel(q["id"])["status"] == "cancelled"
    instance.m.dispatch()
    assert len(instance.calls()) <= 1
    instance.m.stop(r["id"])


def test_queue_revalidates_policy(instance):
    r = instance.start("mode:slow")
    q = instance.start(queue=True)
    instance.m.stop(r["id"])
    instance.config["policy"]["routes"] = []
    instance.save()
    response = instance.m.dispatch()
    assert response["rejected"][0]["id"] == q["id"]
    assert response["rejected"][0]["failure"] == "policy"
    assert len(instance.calls()) <= 1


def test_queue_dispatch_race_launches_once(instance):
    r = instance.start("mode:slow")
    q = instance.start(queue=True)
    instance.m.stop(r["id"])
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: instance.m.dispatch(), range(2)))
    assert instance.finish(q)["status"] == "finished"
    assert len([c for c in instance.calls() if c["run"] == q["id"]]) == 1


@pytest.mark.parametrize(
    "task,status",
    [
        ("mode:quota", "quota"),
        ("mode:auth", "authentication"),
        ("mode:no-final", "failed"),
        ("mode:malformed", "failed"),
        ("mode:neutral-quota", "finished"),
    ],
)
def test_verified_output_gate_and_classification(instance, task, status):
    run = instance.start(task)
    final = instance.finish(run)
    assert final["status"] == status
    if status != "finished":
        with pytest.raises(FleetError):
            instance.m.output(run["id"])
        instance.m.recover(run["id"])
        assert len(instance.calls()) == 1


def test_generation_history_and_stale_supervisor_fencing(instance):
    r = instance.start()
    instance.finish(r)
    old = instance.m.output(r["id"])
    new = instance.m.resume(r["id"], "bounded follow-up", expected_generation=1)
    assert new["id"] == r["id"] and new["generation"] == 2
    instance.finish(new)
    assert instance.m.output(r["id"], 1) == old
    assert instance.m.output(r["id"])["generation"] == 2
    p = subprocess.run(
        [sys.executable, "-m", "harnessfleet.worker", r["id"], "1"], capture_output=True, timeout=5
    )
    assert p.returncode != 0 and instance.m.status(r["id"])["generation"] == 2
    assert len(instance.calls()) == 2


def test_read_only_grant_cannot_delegate_writes(instance):
    run = instance.start("mode:forbidden-child")
    assert instance.finish(run)["status"] == "finished"
    assert instance.m.output(run["id"])["text"] == "Child refusal: permission"
    assert len(instance.calls()) == 1


@pytest.mark.parametrize("permission", ["read-only", "workspace-write"])
def test_codex_state_write_access_is_preserved_on_continuation(instance, permission):
    run = json.loads(
        instance.cli(
            "run", "start", "--route", "codex-local", "--permission", permission, "assignment"
        ).stdout
    )
    instance.finish(run)
    continuation = json.loads(instance.cli("run", "resume", run["id"], "follow-up").stdout)
    assert instance.finish(continuation)["status"] == "finished"
    calls = instance.calls()
    assert len(calls) == 2 and "resume" in calls[1]["args"]
    for call in calls:
        roots = [arg for arg in call["args"] if arg.startswith("sandbox_workspace_write.writable_roots=")]
        if permission == "workspace-write":
            if "--add-dir" in call["args"] and "resume" not in call["args"]:
                assert call["args"][call["args"].index("--add-dir") + 1] == str(instance.path / "state")
                continue
            assert len(roots) == 1
            assert json.loads(roots[0].split("=", 1)[1]) == [str(instance.path / "state")]
        else:
            assert not roots


def test_managed_recursive_delegation(instance):
    run = instance.start("mode:recursive")
    assert instance.finish(run)["status"] == "finished"
    assert instance.m.output(run["id"])["text"] == "Integrated synthetic final"
    children = [r for r in instance.m.list()["runs"] if r["parent_run"] == run["id"]]
    assert len(children) == 1 and children[0]["depth"] == 2


def test_box_origin_has_no_effect(instance, monkeypatch):
    monkeypatch.setenv("HARNESSFLEET_BOX", "yes")
    with pytest.raises(FleetError) as e:
        instance.start()
    assert e.value.code == "box_origin" and not instance.calls()


def test_corrupt_output_never_replays(instance):
    r = instance.start()
    instance.finish(r)
    path = instance.m.store.root / r["id"] / "1" / "output.txt"
    path.write_text("corrupt")
    with pytest.raises(FleetError) as e:
        instance.m.output(r["id"])
    assert e.value.code == "receipt"
    assert len(instance.calls()) == 1


def test_output_symlink_and_traversal_are_refused(instance):
    r = instance.start()
    instance.finish(r)
    path = instance.m.store.root / r["id"] / "1" / "output.txt"
    path.unlink()
    path.symlink_to(instance.path / "config.json")
    with pytest.raises(FleetError):
        instance.m.output(r["id"])
    with pytest.raises(FleetError):
        instance.m.output("../../config")
    assert len(instance.calls()) == 1


def test_stop_preserves_unrelated_sentinel(instance):
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        r = instance.start("mode:slow")
        instance.m.stop(r["id"])
        assert sentinel.poll() is None
        assert instance.m.status(r["id"])["cleanup_confirmed"]
    finally:
        sentinel.terminate()
        sentinel.wait()


def test_detached_descendant_is_cleaned(instance):
    r = instance.start("mode:descendant")
    final = instance.finish(r)
    assert final["cleanup_confirmed"] and final["status"] == "finished"


def test_supervisor_death_recovery_does_not_duplicate_native_work(instance):
    r = instance.start("mode:slow")
    # Await an observable native creation event, rather than watching unchanged logs.
    watcher = processes.Changes(instance.m.store.root)
    try:
        end = time.monotonic() + 5
        while True:
            native = instance.m.store.get(r["id"])
            if native.get("native_process"):
                break
            assert time.monotonic() < end
            watcher.wait(0.5)
        fd = processes.pidfd_open(native["supervisor"]["pid"])
        processes.signal_fd(fd, signal.SIGKILL)
        import select

        assert select.select([fd], [], [], 5)[0]
        os.close(fd)
        instance.m.recover(r["id"])
        final = instance.m.status(r["id"])
        assert final["status"] == "failed" and final["cleanup_confirmed"]
        assert len(instance.calls()) <= 1
    finally:
        watcher.close()


def test_unknown_acknowledgement_retains_capacity(instance):
    r = instance.start()
    instance.finish(r)
    with instance.m.store.transaction() as c:
        record = instance.m.store.get(r["id"], c)
        record.update(
            status="uncertain", native_process=None, supervisor=None, cleanup_confirmed=False, updated_at=0
        )
        instance.m.store.save(c, record, "synthetic_controller_loss")
    instance.m.recover(r["id"])
    assert instance.m.status(r["id"])["status"] == "uncertain"
    with pytest.raises(FleetError) as e:
        instance.start()
    assert e.value.code == "capacity" and len(instance.calls()) == 1


def test_missing_auth_is_separate_from_execution_failure(instance):
    instance.config["routes"][0]["executable"] = "missing-native-binary"
    instance.save()
    r = instance.start()
    final = instance.finish(r)
    assert final["status"] == "authentication" and not final["invoked"]
    assert not instance.calls()


def test_invalid_completion_receipt_never_replays(instance):
    r = instance.start()
    instance.finish(r)
    path = instance.m.store.root / r["id"] / "1" / "receipt.json"
    path.write_text('{"schema_version":1,"accepted":true}')
    with pytest.raises(FleetError) as e:
        instance.m.output(r["id"])
    assert e.value.code == "receipt" and len(instance.calls()) == 1


def test_cancel_dispatch_race_never_launches_cancelled_work(instance):
    r = instance.start("mode:slow")
    q = instance.start(queue=True)
    instance.m.stop(r["id"])

    def cancel():
        try:
            return instance.m.cancel(q["id"])
        except FleetError:
            return None

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(cancel), pool.submit(instance.m.dispatch)]
        for f in futures:
            f.result()
    final = instance.m.status(q["id"])
    if final["status"] == "cancelled":
        assert not [c for c in instance.calls() if c["run"] == q["id"]]
    else:
        assert instance.finish(q)["status"] == "finished"
        assert len([c for c in instance.calls() if c["run"] == q["id"]]) == 1


def test_stop_fences_concurrent_child_admission(instance, monkeypatch):
    parent = instance.start("mode:slow")
    watch = processes.Changes(instance.m.store.root)
    try:
        end = time.monotonic() + 5
        while instance.m.status(parent["id"])["status"] != "running":
            assert time.monotonic() < end
            watch.wait(0.5)
        record = instance.m.store.get(parent["id"])
        monkeypatch.setenv("HARNESSFLEET_PARENT_RUN", parent["id"])
        monkeypatch.setenv("HARNESSFLEET_PARENT_GENERATION", "1")
        monkeypatch.setenv("HARNESSFLEET_PARENT_TOKEN", record["child_token"])

        def admit():
            try:
                return instance.m.start("claude-local", "mode:slow")
            except FleetError as e:
                assert e.code == "parent"
                return None

        with ThreadPoolExecutor(2) as pool:
            admission = pool.submit(admit)
            stopping = pool.submit(instance.m.stop, parent["id"])
            child = admission.result()
            stopping.result()
        assert instance.m.status(parent["id"])["status"] == "stopped"
        if child:
            assert instance.m.status(child["id"])["status"] in ("stopped", "cancelled")
    finally:
        watch.close()


def test_managed_child_continuation_uses_a_new_host_spawn_claim(instance):
    root = instance.start("mode:recursive-resume")
    assert instance.finish(root)["status"] == "finished"
    assert instance.m.output(root["id"])["text"] == "Continued synthetic final"
    children = [r for r in instance.m.list()["runs"] if r["parent_run"] == root["id"]]
    assert len(children) == 1 and children[0]["generation"] == 2
    assert len(instance.calls()) == 3


def test_orphaned_managed_child_continuation_is_refused_before_admission(instance):
    root = instance.start("mode:recursive")
    instance.finish(root)
    child = [r for r in instance.m.list()["runs"] if r["parent_run"] == root["id"]][0]
    with pytest.raises(FleetError) as e:
        instance.m.resume(child["id"], "another bounded assignment")
    assert e.value.code == "parent"
    assert instance.m.status(child["id"])["generation"] == 1
    assert len(instance.calls()) == 2
