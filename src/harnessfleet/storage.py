"""Transactional admission, generation fencing and append-only events."""

import contextlib
import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from .contracts import FleetError

IDENTIFIER = re.compile(r"[a-f0-9]{24}\Z")
MAX_ARTIFACT = 4 * 1024 * 1024


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or self.root.stat().st_uid != os.getuid():
            raise FleetError("state", "State must be a user-owned directory")
        os.chmod(self.root, 0o700)
        self.db = self.root / "fleet.sqlite3"
        if self.db.is_symlink():
            raise FleetError("state", "Database symlinks are refused")
        with self.transaction() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS generations(run_id TEXT, number INTEGER, data TEXT NOT NULL,
                PRIMARY KEY(run_id,number));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
                generation INTEGER, at REAL, kind TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, data TEXT);
            """)
        os.chmod(self.db, 0o600)

    @contextlib.contextmanager
    def transaction(self):
        c = sqlite3.connect(self.db, timeout=15)
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def all(self, c=None):
        if c is None:
            with self.transaction() as c:
                return self.all(c)
        return [json.loads(row[0]) for row in c.execute("SELECT data FROM runs ORDER BY rowid")]

    def get(self, rid, c=None):
        if not isinstance(rid, str) or not IDENTIFIER.fullmatch(rid):
            raise FleetError("run", "Invalid Agent Run identifier")
        if c is None:
            with self.transaction() as c:
                return self.get(rid, c)
        row = c.execute("SELECT data FROM runs WHERE id=?", (rid,)).fetchone()
        if not row:
            raise FleetError("run", "Agent Run does not exist")
        return json.loads(row[0])

    def save(self, c, run, kind, detail=None):
        c.execute("INSERT OR REPLACE INTO runs VALUES (?,?)", (run["id"], json.dumps(run)))
        c.execute(
            "INSERT OR REPLACE INTO generations VALUES (?,?,?)",
            (run["id"], run["generation"], json.dumps(run)),
        )
        self.event(c, run, kind, detail)

    def event(self, c, run, kind, detail=None):
        c.execute(
            "INSERT INTO events(run_id,generation,at,kind,data) VALUES (?,?,?,?,?)",
            (run["id"], run["generation"], time.time(), kind, json.dumps(detail or {})),
        )

    def events(self, rid, after=0, limit=100):
        self.get(rid)
        with self.transaction() as c:
            rows = c.execute(
                "SELECT seq,generation,at,kind,data FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (rid, after, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(seq=r[0], generation=r[1], at=r[2], kind=r[3], data=json.loads(r[4])) for r in rows]

    def generation(self, rid, number):
        self.get(rid)
        with self.transaction() as c:
            row = c.execute(
                "SELECT data FROM generations WHERE run_id=? AND number=?", (rid, number)
            ).fetchone()
        if not row:
            raise FleetError("generation", "Generation does not exist")
        return json.loads(row[0])

    def _artifact_fd(self, rid, generation, create=False):
        if not isinstance(rid, str) or not IDENTIFIER.fullmatch(rid):
            raise FleetError("run", "Invalid Agent Run identifier")
        if type(generation) is not int or generation < 1:
            raise FleetError("generation", "Invalid generation")
        parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in (rid, str(generation)):
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            return parent
        except BaseException:
            os.close(parent)
            raise

    def directory(self, rid, generation):
        try:
            fd = self._artifact_fd(rid, generation, create=True)
            os.close(fd)
            return self.root / rid / str(generation)
        except OSError:
            raise FleetError("artifact", "Unsafe artifact directory")

    def create_log(self, rid, generation):
        parent = self._artifact_fd(rid, generation, create=True)
        try:
            return os.open(
                "supervisor.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
        finally:
            os.close(parent)

    def write_artifact(self, rid, gen, name, content):
        if name not in ("output.txt", "partial.txt", "events.jsonl", "stderr.txt", "receipt.json"):
            raise FleetError("artifact", "Unknown artifact")
        import secrets

        parent = self._artifact_fd(rid, gen, create=True)
        temporary = ".write-" + secrets.token_hex(8)
        try:
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            with os.fdopen(fd, "wb") as f:
                f.write(content.encode()[:MAX_ARTIFACT])
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)

    def read_artifact(self, rid, gen, name):
        if name not in ("output.txt", "partial.txt", "events.jsonl", "stderr.txt", "receipt.json"):
            raise FleetError("artifact", "Unknown artifact")
        try:
            parent = self._artifact_fd(rid, gen)
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            finally:
                os.close(parent)
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    raise FleetError("artifact", "Artifact must be a regular file")
                data = f.read(MAX_ARTIFACT + 1)
            if len(data) > MAX_ARTIFACT:
                raise FleetError("artifact", "Artifact exceeds bounded read limit")
            return data.decode("utf-8", errors="replace")
        except OSError:
            raise FleetError("artifact", "Artifact is absent, expired or unsafe")
