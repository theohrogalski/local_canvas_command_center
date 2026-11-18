"""Local send ledger (SQLite) - the core duplicate-prevention mechanism.

Every send is *reserved* here before the POST is made. A UNIQUE constraint on
(kind, source course, source item, target course) means a second reservation
for the same combination is impossible, even from two programs at once.

Row status:
  pending  - reserved; POST started but outcome not confirmed (blocks re-send)
  sent     - Canvas confirmed creation (blocks re-send forever)
  failed   - Canvas definitely rejected it (4xx), nothing created; may be retried

The ledger also records the time of every POST attempt so the hourly rate
limit survives restarts.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sends (
    id              INTEGER PRIMARY KEY,
    kind            TEXT    NOT NULL CHECK (kind IN ('announcement', 'assignment', 'file')),
    source_course   INTEGER NOT NULL,
    source_item     INTEGER NOT NULL,
    target_course   INTEGER NOT NULL,
    title           TEXT    NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    created_item_id INTEGER,
    detail          TEXT,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    UNIQUE (kind, source_course, source_item, target_course)
);
CREATE TABLE IF NOT EXISTS post_attempts (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS visibility_changes (
    id          INTEGER PRIMARY KEY,
    course_id   INTEGER NOT NULL,
    course_name TEXT    NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('assignment', 'file')),
    item_id     INTEGER NOT NULL,
    item_name   TEXT    NOT NULL,
    before      TEXT    NOT NULL,
    after       TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    detail      TEXT,
    created_at  REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS grading_runs (
    id                  INTEGER PRIMARY KEY,
    action              TEXT    NOT NULL CHECK (action IN ('apply', 'restore')),
    course_id           INTEGER NOT NULL,
    course_name         TEXT    NOT NULL,
    source              TEXT    NOT NULL,
    status              TEXT    NOT NULL,
    changes             INTEGER NOT NULL,
    submissions_checked INTEGER NOT NULL,
    snapshot_path       TEXT,
    detail              TEXT,
    created_at          REAL    NOT NULL
);
"""


@dataclass(frozen=True)
class VisibilityChange:
    id: int
    course_id: int
    course_name: str
    kind: str
    item_id: int
    item_name: str
    before: str
    after: str
    status: str
    detail: str | None
    created_at: float


@dataclass(frozen=True)
class GradingRun:
    id: int
    action: str
    course_id: int
    course_name: str
    source: str
    status: str
    changes: int
    submissions_checked: int
    snapshot_path: str | None
    detail: str | None
    created_at: float


class AlreadyRecorded(Exception):
    def __init__(self, row: "LedgerRow"):
        super().__init__(f"Already {row.status} (ledger #{row.id})")
        self.row = row


@dataclass(frozen=True)
class SendKey:
    kind: str
    source_course: int
    source_item: int
    target_course: int


@dataclass(frozen=True)
class LedgerRow:
    id: int
    kind: str
    source_course: int
    source_item: int
    target_course: int
    title: str
    status: str
    created_item_id: int | None
    detail: str | None
    created_at: float
    updated_at: float


class Ledger:
    def __init__(self, path: str | Path, clock=time.time):
        self.path = str(path)
        self._clock = clock
        new = not os.path.exists(self.path)
        if new:  # create with owner-only permissions
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        self._db = sqlite3.connect(self.path, isolation_level=None, timeout=30,
                                   check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._upgrade_sends_kinds()
        self._db.executescript(SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(post_attempts)")}
        if "channel" not in cols:   # upgrade ledgers created by version 1.0
            self._db.execute("ALTER TABLE post_attempts ADD COLUMN channel TEXT NOT NULL DEFAULT 'post'")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _upgrade_sends_kinds(self):
        """Ledgers made before file uploads existed only allow two kinds. SQLite
        cannot change a CHECK constraint in place, so copy the rows into a new
        table inside one transaction (all or nothing)."""
        row = self._db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sends'").fetchone()
        if not row or "'file'" in row[0]:
            return
        new_table = SCHEMA.split("CREATE TABLE IF NOT EXISTS post_attempts")[0].replace(
            "CREATE TABLE IF NOT EXISTS sends (", "CREATE TABLE sends_upgrade (")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(new_table)
            self._db.execute("INSERT INTO sends_upgrade SELECT * FROM sends")
            before = self._db.execute("SELECT COUNT(*) FROM sends").fetchone()[0]
            after = self._db.execute("SELECT COUNT(*) FROM sends_upgrade").fetchone()[0]
            if before != after:
                raise RuntimeError("ledger upgrade row count mismatch")
            self._db.execute("DROP TABLE sends")
            self._db.execute("ALTER TABLE sends_upgrade RENAME TO sends")
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def close(self):
        self._db.close()

    # ---------------------------------------------------------------- reads
    def find(self, key: SendKey) -> LedgerRow | None:
        cur = self._db.execute(
            "SELECT * FROM sends WHERE kind=? AND source_course=? AND source_item=? AND target_course=?",
            (key.kind, key.source_course, key.source_item, key.target_course))
        row = cur.fetchone()
        return LedgerRow(*row) if row else None

    def all_rows(self) -> list[LedgerRow]:
        return [LedgerRow(*r) for r in self._db.execute("SELECT * FROM sends ORDER BY id DESC")]

    # --------------------------------------------------------------- writes
    def reserve(self, key: SendKey, title: str) -> int:
        """Atomically claim this send. Raises AlreadyRecorded if it exists
        (unless the earlier attempt definitely failed)."""
        now = self._clock()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.find(key)
            if existing is None:
                cur = self._db.execute(
                    "INSERT INTO sends (kind, source_course, source_item, target_course, title,"
                    " status, created_at, updated_at) VALUES (?,?,?,?,?, 'pending', ?, ?)",
                    (key.kind, key.source_course, key.source_item, key.target_course, title, now, now))
                row_id = cur.lastrowid
            elif existing.status == "failed":
                self._db.execute(
                    "UPDATE sends SET status='pending', detail=NULL, updated_at=? WHERE id=? AND status='failed'",
                    (now, existing.id))
                row_id = existing.id
            else:
                raise AlreadyRecorded(existing)
            self._db.execute("COMMIT")
            return row_id
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def mark_sent(self, row_id: int, created_item_id: int | None):
        self._db.execute("UPDATE sends SET status='sent', created_item_id=?, detail=NULL, updated_at=? WHERE id=?",
                         (created_item_id, self._clock(), row_id))

    def mark_failed(self, row_id: int, detail: str):
        self._db.execute("UPDATE sends SET status='failed', detail=?, updated_at=? WHERE id=?",
                         (detail, self._clock(), row_id))

    def note_pending(self, row_id: int, detail: str):
        self._db.execute("UPDATE sends SET detail=?, updated_at=? WHERE id=? AND status='pending'",
                         (detail, self._clock(), row_id))

    def release(self, row_id: int) -> bool:
        """Delete a row so the item may be sent again. Only for use after the
        teacher has checked in Canvas that the item does NOT exist."""
        cur = self._db.execute("DELETE FROM sends WHERE id=?", (row_id,))
        return cur.rowcount == 1

    # ------------------------------------------------------ rate-limit data
    # "channel" separates announcement/assignment posts ("post") from grading
    # edits ("grading") so each has its own limits.
    def record_attempt(self, ts: float | None = None, channel: str = "post"):
        self._db.execute("INSERT INTO post_attempts (ts, channel) VALUES (?, ?)",
                         (self._clock() if ts is None else ts, channel))

    def attempts_since(self, ts: float, channel: str = "post") -> int:
        return self._db.execute("SELECT COUNT(*) FROM post_attempts WHERE ts >= ? AND channel = ?",
                                (ts, channel)).fetchone()[0]

    def last_attempt(self, channel: str = "post") -> float | None:
        return self._db.execute("SELECT MAX(ts) FROM post_attempts WHERE channel = ?", (channel,)).fetchone()[0]

    # ------------------------------------------------------- grading audit
    def record_grading_run(self, action, course_id, course_name, source, status, changes,
                           submissions_checked, snapshot_path, detail=None) -> int:
        cur = self._db.execute(
            "INSERT INTO grading_runs (action, course_id, course_name, source, status, changes,"
            " submissions_checked, snapshot_path, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (action, course_id, course_name, source, status, changes, submissions_checked,
             snapshot_path, detail, self._clock()))
        return cur.lastrowid

    # ---------------------------------------------- publish/unpublish audit
    def record_visibility(self, course_id, course_name, kind, item_id, item_name, before, after,
                          status, detail=None) -> int:
        cur = self._db.execute(
            "INSERT INTO visibility_changes (course_id, course_name, kind, item_id, item_name, before, after,"
            " status, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, course_name, kind, item_id, item_name, before, after, status, detail, self._clock()))
        return cur.lastrowid

    def visibility_changes(self) -> list[VisibilityChange]:
        return [VisibilityChange(*r) for r in
                self._db.execute("SELECT * FROM visibility_changes ORDER BY id DESC")]

    def grading_runs(self) -> list[GradingRun]:
        return [GradingRun(*r) for r in self._db.execute("SELECT * FROM grading_runs ORDER BY id DESC")]
