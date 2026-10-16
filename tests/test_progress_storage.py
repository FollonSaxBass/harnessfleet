"""Progress batch storage through real runs and their public status/event records."""

import time

import pytest

from harnessfleet import processes


@pytest.fixture
def active(instance):
    run = instance.m.start("codex-local", "mode:slow", delegation="leaf")
    watch = processes.Changes(instance.m.store.root)
    try:
        deadline = time.monotonic() + 5
        while not instance.m.status(run["id"])["session_id"]:
            assert time.monotonic() < deadline
            watch.wait(0.5)
        yield instance, run
    finally:
        watch.close()
        instance.m.stop(run["id"])


def test_progress_batch_preserves_order_session_and_current_run_fields(active):
    instance, run = active
    before = instance.m.events(run["id"])["events"][-1]["seq"]
    instance.m.keep(run["id"])
    instance.m.store.persist_progress_batch(
        run["id"], run["generation"], [("item.started", None), ("item.completed", "synthetic-session")]
    )
    status = instance.m.status(run["id"])
    assert status["session_id"] == "synthetic-session" and status["kept"]
    events = [e for e in instance.m.events(run["id"], before)["events"] if e["kind"] == "progress"]
    assert [e["data"]["type"] for e in events] == ["item.started", "item.completed"]
    assert all(e["generation"] == run["generation"] for e in events)


def test_progress_batch_rejects_cancelled_or_terminal_run(active):
    import threading
    from harnessfleet import FleetError

    instance, run = active
    after = instance.m.events(run["id"])["events"][-1]["seq"]
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(FleetError) as error:
        instance.m.store.persist_progress_batch(
            run["id"], run["generation"], [("cancelled-event", "replacement")], cancelled=cancelled.is_set
        )
    assert error.value.code == "cancelled"
    assert not instance.m.events(run["id"], after)["events"]
    instance.m.stop(run["id"])
    terminal = instance.m.status(run["id"])
    after = instance.m.events(run["id"])["events"][-1]["seq"]
    with pytest.raises(FleetError) as error:
        instance.m.store.persist_progress_batch(run["id"], run["generation"], [("late", "replacement")])
    assert error.value.code == "generation"
    assert instance.m.status(run["id"]) == terminal
    assert not instance.m.events(run["id"], after)["events"]


def test_old_progress_cannot_overwrite_a_continued_generation(instance):
    from harnessfleet import FleetError

    first = instance.m.start("codex-local", "mode:normal", delegation="leaf")
    assert instance.finish(first)["accepted"]
    current = instance.m.resume(first["id"], "mode:slow")
    watch = processes.Changes(instance.m.store.root)
    try:
        deadline = time.monotonic() + 5
        while instance.m.status(current["id"])["status"] != "running":
            assert time.monotonic() < deadline
            watch.wait(0.5)
        with pytest.raises(FleetError) as error:
            instance.m.store.persist_progress_batch(
                first["id"], first["generation"], [("old", "old-session")]
            )
        assert error.value.code == "generation"
        assert instance.m.status(current["id"])["generation"] == 2
    finally:
        watch.close()
        instance.m.stop(current["id"])


def test_busy_progress_retries_only_after_no_changes_committed(active):
    from harnessfleet import FleetError

    instance, run = active
    original_session = instance.m.status(run["id"])["session_id"]
    after = instance.m.events(run["id"])["events"][-1]["seq"]
    records = [("after-lock", "replacement")]
    start = time.monotonic()
    with instance.m.store.transaction():
        with pytest.raises(FleetError) as error:
            instance.m.store.persist_progress_batch(run["id"], run["generation"], records, timeout=0.03)
    assert error.value.code == "progress_busy"
    assert time.monotonic() - start < 0.5
    assert instance.m.status(run["id"])["session_id"] == original_session
    assert not instance.m.events(run["id"], after)["events"]
    instance.m.store.persist_progress_batch(run["id"], run["generation"], records)
    assert [e["data"]["type"] for e in instance.m.events(run["id"], after)["events"]] == ["after-lock"]


def test_failing_progress_batch_rolls_back_run_and_all_events(active):
    import sqlite3

    instance, run = active
    original_session = instance.m.status(run["id"])["session_id"]
    after = instance.m.events(run["id"])["events"][-1]["seq"]
    with instance.m.store.transaction() as connection:
        connection.execute("""CREATE TRIGGER reject_bad_progress BEFORE INSERT ON events
            WHEN NEW.kind='progress' AND NEW.data LIKE '%bad%'
            BEGIN SELECT RAISE(ABORT, 'synthetic progress failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        instance.m.store.persist_progress_batch(
            run["id"], run["generation"], [("good", "replacement"), ("bad", "replacement")]
        )
    assert instance.m.status(run["id"])["session_id"] == original_session
    assert not instance.m.events(run["id"], after)["events"]


def test_ambiguous_commit_acknowledgement_is_not_retryable(active, monkeypatch):
    import sqlite3
    from harnessfleet import storage

    instance, run = active
    after = instance.m.events(run["id"])["events"][-1]["seq"]
    connect = sqlite3.connect

    class LostAcknowledgement:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            self.connection.commit()
            error = sqlite3.OperationalError("synthetic lost acknowledgement")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error

    with monkeypatch.context() as patch:
        patch.setattr(storage.sqlite3, "connect", lambda *a, **kw: LostAcknowledgement(connect(*a, **kw)))
        with pytest.raises(sqlite3.OperationalError):
            instance.m.store.persist_progress_batch(run["id"], run["generation"], [("once", "replacement")])
    assert [e["data"]["type"] for e in instance.m.events(run["id"], after)["events"]] == ["once"]
