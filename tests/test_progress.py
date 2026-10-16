"""Private writer seam tests; these do not certify native acceptance or Store SQL."""

import json
import os
import select
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from harnessfleet.contracts import FleetError
from harnessfleet import progress
from harnessfleet.progress import ProgressWriter


class Boundary:
    """Synthetic persistence boundary with controlled I/O and commit witnesses."""

    def __init__(self, action=None):
        self.action = action
        self.calls = []
        self.committed = []
        self.entered = threading.Event()
        self.committed_event = threading.Event()

    def persist_progress_batch(self, rid, generation, records, timeout=0.05, cancelled=None):
        assert (rid, generation) == ("run", 7)
        assert 0 < timeout <= 0.05
        assert callable(cancelled)
        self.calls.append(list(records))
        self.entered.set()
        if self.action:
            self.action(self, cancelled)
        if not cancelled():
            self.committed.extend(records)
            self.committed_event.set()


class ProgressTests(unittest.TestCase):
    def writer(self, boundary):
        writer = ProgressWriter(boundary, "run", 7)

        def cleanup():
            writer.cancel()
            self.assertTrue(writer.join(1), "Test left a progress thread running")

        self.addCleanup(cleanup)
        return writer

    def blocked(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def block(boundary, cancelled):
            if not release.wait(2):
                raise AssertionError("Test did not release synthetic filesystem call")

        return Boundary(block), release

    def test_ordered_bounded_batches_and_finish_closes_admissions(self):
        boundary, release = self.blocked()
        writer = self.writer(boundary)
        expected = [("first", "session")]
        writer.submit(*expected[0])
        self.assertTrue(boundary.entered.wait(1))
        for i in range(400):
            record = (f"kind-{i}", "session")
            expected.append(record)
            writer.submit(*record)
        release.set()
        writer.finish()
        self.assertEqual(boundary.committed, expected)
        self.assertTrue(all(1 <= len(batch) <= 128 for batch in boundary.calls))
        self.assertTrue(writer.join())
        with self.assertRaises(FleetError) as caught:
            writer.submit("late", None)
        self.assertEqual(caught.exception.code, "progress_closed")
        writer.finish()

    def test_count_limit_includes_in_flight_and_submit_does_not_wait(self):
        boundary, release = self.blocked()
        writer = self.writer(boundary)
        writer.submit("first", "session")
        self.assertTrue(boundary.entered.wait(1))
        started = time.monotonic()
        for _ in range(4095):
            writer.submit("event", None)
        with self.assertRaises(FleetError) as caught:
            writer.submit("overflow", None)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(caught.exception.code, "progress_overflow")
        self.assertEqual(len(boundary.calls), 1)
        self.assertTrue(select.select([writer.failure_fd], [], [], 0)[0])
        started = time.monotonic()
        with self.assertRaises(FleetError) as failure:
            writer.finish(0.5)
        self.assertIs(failure.exception, caught.exception)
        self.assertLess(time.monotonic() - started, 0.2)
        release.set()
        self.assertTrue(writer.join())
        self.assertEqual(boundary.committed, [])
        with self.assertRaises(FleetError) as failure:
            writer.raise_if_failed()
        self.assertIs(failure.exception, caught.exception)

    def test_encoded_byte_limit_counts_in_flight(self):
        boundary, release = self.blocked()
        writer = self.writer(boundary)
        # JSON escapes make this snapshot larger than its Python character count.
        session = "é" * 100_000
        size = len(json.dumps(("event", session), ensure_ascii=True, separators=(",", ":")).encode())
        self.assertLess(3 * size, 2 * 1024 * 1024)
        self.assertGreater(4 * size, 2 * 1024 * 1024)
        writer.submit("event", session)
        self.assertTrue(boundary.entered.wait(1))
        writer.submit("event", session)
        writer.submit("event", session)
        with self.assertRaises(FleetError) as caught:
            writer.submit("event", session)
        self.assertEqual(caught.exception.code, "progress_overflow")
        release.set()

    def test_only_explicit_rolled_back_busy_is_retried_in_order(self):
        def busy_twice(boundary, cancelled):
            if len(boundary.calls) < 3:
                raise FleetError("progress_busy", "Confirmed rollback")

        boundary = Boundary(busy_twice)
        writer = self.writer(boundary)
        writer.submit("first", "session")
        self.assertTrue(boundary.entered.wait(1))
        writer.submit("second", None)
        writer.finish()
        self.assertEqual(boundary.calls[:3], [[("first", "session")]] * 3)
        self.assertEqual(boundary.committed, [("first", "session"), ("second", None)])

    def test_errors_and_ambiguous_commit_are_not_retried_and_wake_idle_reader(self):
        errors = [
            FleetError("state", "Generation changed"),
            sqlite3.OperationalError("database is locked"),
            OSError("Ambiguous commit I/O failure"),
        ]
        for error in errors:
            with self.subTest(error=error):

                def fail(boundary, cancelled):
                    # Simulate an exception after the commit may have happened.
                    boundary.committed.extend(boundary.calls[-1])
                    raise error

                boundary = Boundary(fail)
                writer = self.writer(boundary)
                fd = writer.failure_fd
                writer.submit("event", "session")
                self.assertEqual(select.select([fd], [], [], 1)[0], [fd])
                with self.assertRaises(type(error)) as caught:
                    writer.finish()
                self.assertIs(caught.exception, error)
                self.assertEqual(len(boundary.calls), 1)
                self.assertTrue(writer.join())

    def test_oldest_record_deadline_is_not_reset_by_retry(self):
        def busy(boundary, cancelled):
            raise FleetError("progress_busy", "Confirmed rollback")

        with patch.object(progress, "_MAX_AGE", 0.12):
            boundary = Boundary(busy)
            writer = self.writer(boundary)
            started = time.monotonic()
            writer.submit("event", "session")
            with self.assertRaises(FleetError) as caught:
                writer.finish(1)
            self.assertEqual(caught.exception.code, "progress_timeout")
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertGreater(len(boundary.calls), 1)
            self.assertEqual(boundary.committed, [])

    def test_cancel_interrupts_retry_wait_and_discards_pending(self):
        def busy(boundary, cancelled):
            raise FleetError("progress_busy", "Confirmed rollback")

        with patch.object(progress, "_RETRY_INTERVAL", 2):
            boundary = Boundary(busy)
            writer = self.writer(boundary)
            writer.submit("first", "session")
            self.assertTrue(boundary.entered.wait(1))
            writer.submit("pending", None)
            writer.cancel()
            writer.cancel()
            self.assertTrue(writer.join(0.5))
            self.assertEqual(len(boundary.calls), 1)
            self.assertEqual(boundary.committed, [])
            with self.assertRaises(FleetError) as caught:
                writer.finish()
            self.assertEqual(caught.exception.code, "progress_cancelled")

    def test_active_io_timeout_and_cancel_do_not_claim_thread_stopped(self):
        boundary, release = self.blocked()
        writer = self.writer(boundary)
        self.assertFalse(writer._thread.daemon)
        fd = writer.failure_fd
        writer.submit("event", "session")
        self.assertTrue(boundary.entered.wait(1))
        with self.assertRaises(FleetError) as caught:
            writer.finish(0.01)
        self.assertEqual(caught.exception.code, "progress_timeout")
        writer.cancel()
        self.assertFalse(writer.join(0.01))
        self.assertEqual(writer.failure_fd, fd)
        os.fstat(fd)
        release.set()
        self.assertTrue(writer.join())
        self.assertIsNone(writer.failure_fd)
        # A repeated join must not close an unrelated reused descriptor.
        read_fd, write_fd = os.pipe()
        try:
            self.assertTrue(writer.join())
            os.fstat(read_fd)
            os.fstat(write_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)
        self.assertEqual(boundary.committed, [])

    def test_idle_finish_and_cancel_join_are_idempotent(self):
        writer = self.writer(Boundary())
        self.assertFalse(writer.join(0))
        writer.finish()
        self.assertTrue(writer.join())
        self.assertTrue(writer.join())
        writer.cancel()
        writer.cancel()

    def test_flush_timer_new_session_and_finish_schedule(self):
        with patch.object(progress, "_FLUSH_INTERVAL", 10):
            boundary = Boundary()
            writer = self.writer(boundary)
            writer.submit("ordinary", None)
            self.assertFalse(boundary.entered.wait(0.02))
            writer.submit("session", "new-session")
            self.assertTrue(boundary.committed_event.wait(1))
            self.assertEqual(boundary.committed, [("ordinary", None), ("session", "new-session")])
            writer.submit("final", "new-session")
            writer.finish(1)
            self.assertEqual(boundary.committed[-1], ("final", "new-session"))
        boundary = Boundary()
        writer = self.writer(boundary)
        started = time.monotonic()
        writer.submit("ordinary", None)
        self.assertTrue(boundary.committed_event.wait(0.5))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_input_validation_and_immutable_snapshots(self):
        for kind, session in [(None, None), ("x" * 81, None), ("event", []), (12, "s")]:
            with self.subTest(kind=kind, session=session):
                boundary = Boundary()
                writer = self.writer(boundary)
                with self.assertRaises(FleetError) as caught:
                    writer.submit(kind, session)
                self.assertEqual(caught.exception.code, "protocol")
                self.assertTrue(select.select([writer.failure_fd], [], [], 1)[0])
                self.assertTrue(writer.join())
                self.assertEqual(boundary.calls, [])
        boundary = Boundary()
        writer = self.writer(boundary)
        writer.submit("x" * 80, "session")
        writer.submit("", None)
        writer.finish()
        self.assertEqual(boundary.committed, [("x" * 80, "session"), ("", None)])

    def test_success_after_record_deadline_is_still_a_failure(self):
        def delayed_success(boundary, cancelled):
            time.sleep(0.05)

        with patch.object(progress, "_MAX_AGE", 0.02):
            boundary = Boundary(delayed_success)
            writer = self.writer(boundary)
            writer.submit("late", "session")
            with self.assertRaises(FleetError) as caught:
                writer.finish(1)
            self.assertEqual(caught.exception.code, "progress_timeout")
            self.assertEqual(len(boundary.calls), 1)


if __name__ == "__main__":
    unittest.main()
