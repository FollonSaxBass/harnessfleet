"""Registered helper receipts remain ownership evidence after environment scrubbing."""

import subprocess
import sys

from harnessfleet import processes


def test_stop_cleans_registered_detached_process_without_marker(instance):
    run = instance.start()
    assert instance.finish(run)["status"] == "finished"
    helper = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"], env={}, start_new_session=True
    )
    try:
        with instance.m.store.transaction() as c:
            record = instance.m.store.get(run["id"], c)
            record.update(
                status="uncertain", cleanup_confirmed=False, owned_processes=[processes.receipt(helper.pid)]
            )
            instance.m.store.save(c, record, "synthetic_registered_helper")
        final = instance.m.stop(run["id"])
        assert final["cleanup_confirmed"] and final["status"] == "stopped"
        assert helper.wait(timeout=1) < 0
    finally:
        if helper.poll() is None:
            helper.kill()
        helper.wait()


def test_stale_registered_identity_preserves_unrelated_sentinel(instance):
    run = instance.start()
    assert instance.finish(run)["status"] == "finished"
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"], env={}, start_new_session=True
    )
    try:
        with instance.m.store.transaction() as c:
            record = instance.m.store.get(run["id"], c)
            record.update(
                status="uncertain",
                cleanup_confirmed=False,
                owned_processes=[{"pid": sentinel.pid, "birth": "retired-process-birth"}],
            )
            instance.m.store.save(c, record, "synthetic_stale_helper_receipt")
        final = instance.m.stop(run["id"])
        assert final["cleanup_confirmed"] and sentinel.poll() is None
    finally:
        sentinel.kill()
        sentinel.wait()


def test_unreadable_registered_identity_retains_uncertainty(instance, monkeypatch):
    run = instance.start()
    assert instance.finish(run)["status"] == "finished"
    helper = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"], env={}, start_new_session=True
    )
    try:
        with instance.m.store.transaction() as c:
            record = instance.m.store.get(run["id"], c)
            record.update(
                status="uncertain", cleanup_confirmed=False, owned_processes=[processes.receipt(helper.pid)]
            )
            instance.m.store.save(c, record, "synthetic_registered_helper")
        original = processes.receipt

        def unreadable(pid):
            if pid == helper.pid:
                raise PermissionError("Synthetic inaccessible registered helper")
            return original(pid)

        monkeypatch.setattr(processes, "receipt", unreadable)
        final = instance.m.stop(run["id"])
        assert final["status"] == "uncertain" and not final["cleanup_confirmed"]
        assert helper.poll() is None
    finally:
        helper.kill()
        helper.wait()
