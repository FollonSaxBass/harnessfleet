"""Bounded asynchronous progress persistence for one run generation."""

from collections import deque
import json
import os
import threading
import time

from .contracts import FleetError

_MAX_RECORDS = 4096
_MAX_BYTES = 2 * 1024 * 1024
_BATCH_SIZE = 128
_FLUSH_INTERVAL = 0.05
_MAX_AGE = 5.0
_RETRY_INTERVAL = 0.05


class ProgressWriter:
    """Own snapshots and a writer thread, never the reader's mutable Result.

    Store must check the cancellation callback before committing. Cancellation
    cannot interrupt an active filesystem syscall. The reader must close its
    selector before join(), which closes the owned failure pipe once dead.
    """

    def __init__(self, store, rid, generation):
        self._store = store
        self._rid = rid
        self._generation = generation
        self._condition = threading.Condition()
        self._cancelled = threading.Event()
        self._pending = deque()
        self._count = 0
        self._bytes = 0
        self._session = None
        self._closed = False
        self._done = False
        self._error = None
        self._read_fd, self._write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._thread = threading.Thread(target=self._run, name="progress-writer", daemon=False)
        try:
            self._thread.start()
        except BaseException:
            os.close(self._read_fd)
            os.close(self._write_fd)
            raise

    @property
    def failure_fd(self):
        with self._condition:
            return self._read_fd

    def _discard_pending(self):
        for _, _, size, _, _ in self._pending:
            self._count -= 1
            self._bytes -= size
        self._pending.clear()

    def _fail(self, error):
        # Caller holds the condition; only the first error signals the pipe.
        if self._error is None:
            self._error = error
            try:
                os.write(self._write_fd, b"!")
            except BlockingIOError:
                pass
        self._closed = True
        self._cancelled.set()
        self._discard_pending()
        self._condition.notify_all()

    def submit(self, kind, session_id):
        """Copy bounded scalar inputs; never wait for persistence or capacity."""
        with self._condition:
            if self._error is not None:
                raise self._error
            if self._closed:
                raise FleetError("progress_closed", "Progress admissions are closed")
            if (
                type(kind) is not str
                or len(kind) > 80
                or (session_id is not None and type(session_id) is not str)
            ):
                self._fail(FleetError("protocol", "Invalid progress snapshot"))
                raise self._error
            # Bound encoding work before allocating a JSON copy of a large input.
            if session_id is not None and len(session_id) > _MAX_BYTES:
                self._fail(FleetError("progress_overflow", "Progress snapshot exceeds byte limit"))
                raise self._error
            size = len(json.dumps((kind, session_id), ensure_ascii=True, separators=(",", ":")).encode())
            if self._count >= _MAX_RECORDS or self._bytes + size > _MAX_BYTES:
                self._fail(FleetError("progress_overflow", "Outstanding progress exceeds bounded limits"))
                raise self._error
            urgent = session_id is not None and session_id != self._session
            if session_id is not None:
                self._session = session_id
            self._pending.append((kind, session_id, size, time.monotonic(), urgent))
            self._count += 1
            self._bytes += size
            self._condition.notify()

    def raise_if_failed(self):
        with self._condition:
            if self._error is not None:
                raise self._error

    def cancel(self):
        """Close admissions and discard pending work; active I/O may still run."""
        with self._condition:
            self._closed = True
            self._cancelled.set()
            self._discard_pending()
            self._condition.notify_all()

    def finish(self, timeout=5.0):
        """Close admissions and wait for every snapshot, raising on failure."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            while not self._done and self._error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fail(
                        FleetError("progress_timeout", "Progress writer did not finish before timeout")
                    )
                    break
                self._condition.wait(remaining)
            if self._error is not None:
                raise self._error
            if self._cancelled.is_set():
                raise FleetError("progress_cancelled", "Progress persistence was cancelled")

    def join(self, timeout=1.0) -> bool:
        """Return confirmed termination; only then release the owned pipe."""
        self._thread.join(timeout=max(0.0, timeout))
        if self._thread.is_alive():
            return False
        with self._condition:
            if self._read_fd is not None:
                os.close(self._read_fd)
                os.close(self._write_fd)
                self._read_fd = self._write_fd = None
        return True

    def _next_batch(self):
        with self._condition:
            while not self._cancelled.is_set():
                if self._pending:
                    delay = self._pending[0][3] + _FLUSH_INTERVAL - time.monotonic()
                    if (
                        self._closed
                        or len(self._pending) >= _BATCH_SIZE
                        or any(record[4] for record in self._pending)
                        or delay <= 0
                    ):
                        return [self._pending.popleft() for _ in range(min(len(self._pending), _BATCH_SIZE))]
                    self._condition.wait(delay)
                elif self._closed:
                    return None
                else:
                    self._condition.wait()
        return None

    def _persist(self, batch):
        records = [(kind, session) for kind, session, _, _, _ in batch]
        deadline = batch[0][3] + _MAX_AGE
        while not self._cancelled.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FleetError("progress_timeout", "Oldest progress snapshot exceeded persistence deadline")
            try:
                self._store.persist_progress_batch(
                    self._rid,
                    self._generation,
                    records,
                    timeout=min(0.05, remaining),
                    cancelled=self._cancelled.is_set,
                )
                if time.monotonic() > deadline:
                    raise FleetError("progress_timeout", "Progress persistence completed after its deadline")
                return
            except FleetError as error:
                if error.code != "progress_busy":
                    raise
            self._cancelled.wait(min(_RETRY_INTERVAL, max(0.0, deadline - time.monotonic())))

    def _run(self):
        try:
            while (batch := self._next_batch()) is not None:
                try:
                    self._persist(batch)
                finally:
                    with self._condition:
                        self._count -= len(batch)
                        self._bytes -= sum(record[2] for record in batch)
        except BaseException as error:
            with self._condition:
                self._fail(error)
        finally:
            with self._condition:
                self._done = True
                self._condition.notify_all()
