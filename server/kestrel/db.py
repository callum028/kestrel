from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    kind     TEXT NOT NULL,
    actor    TEXT NOT NULL,
    task_id  TEXT,
    payload  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, seq);

-- Projection of the event log. Not written to outside the task store.
CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,
    handle           TEXT NOT NULL UNIQUE,
    goal             TEXT NOT NULL,
    criteria         TEXT NOT NULL,
    scope            TEXT,
    executor         TEXT NOT NULL,
    state            TEXT NOT NULL,
    ticket_ref       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_progress_at TEXT,
    progress_hash    TEXT,
    nudges           INTEGER NOT NULL DEFAULT 0
);

-- Attention is unbounded, action is bounded. Observations are what looking past
-- the brief produces. fingerprint makes dismissal durable, so the same thing is
-- never raised twice - without which a proactive system becomes a nagging one.
CREATE TABLE IF NOT EXISTS observations (
    id          TEXT PRIMARY KEY,
    task_id     TEXT,
    what        TEXT NOT NULL,
    location    TEXT,
    why         TEXT,
    state       TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

-- Usage stats only. Durable memory itself lives as markdown in a git repo -
-- this is the sidecar, so that reading a memory does not produce a commit.
-- Used by the review surface, never as truth.
CREATE TABLE IF NOT EXISTS memory_usage (
    id           TEXT PRIMARY KEY,
    last_used_at TEXT NOT NULL,
    uses         INTEGER NOT NULL DEFAULT 1
);

-- One row per thing said to Callum. Escalation is on silence rather than a
-- fan-out: duplicate notifications train you to ignore both.
CREATE TABLE IF NOT EXISTS deliveries (
    id          TEXT PRIMARY KEY,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    urgency     TEXT NOT NULL,
    channel     TEXT NOT NULL,
    task_id     TEXT,
    sent_at     TEXT NOT NULL,
    acked_at    TEXT,
    acked_on    TEXT,
    escalations INTEGER NOT NULL DEFAULT 0
);

-- The dev environment is a shared singleton, so it is a lock rather than a
-- step. Two tasks cannot validate at once; the second would be testing the
-- first one's code.
CREATE TABLE IF NOT EXISTS dev_lock (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    task_id     TEXT,
    acquired_at TEXT
);
CREATE TABLE IF NOT EXISTS dev_lock_queue (
    task_id   TEXT PRIMARY KEY,
    queued_at TEXT NOT NULL
);
INSERT OR IGNORE INTO dev_lock (id, task_id, acquired_at) VALUES (1, NULL, NULL);

-- Claude Code hooks identify themselves by session, not by task. The executor
-- registers the mapping when it starts a session; without it a hook is still
-- recorded, just unattributed - never dropped.
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    started_at TEXT NOT NULL
);

-- Notion is the source of truth for tasks; this is the sidecar that makes the
-- sync loop-safe. last_pushed_lane is what Kestrel itself last wrote - the
-- poller compares against it, not against the task's own state, so its own
-- write never gets read back on the next poll as a manual instruction.
CREATE TABLE IF NOT EXISTS board_sync (
    ticket_id       TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    last_pushed_lane TEXT,
    last_pushed_flag INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);

-- Single-row cursor for both halves of the sync loop, mirroring dev_lock's
-- shape: last_poll_at drives "changed since" for manual-move detection,
-- last_pushed_seq drives how far the event log has been mirrored to Notion.
CREATE TABLE IF NOT EXISTS board_cursor (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_poll_at    TEXT,
    last_pushed_seq INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO board_cursor (id, last_poll_at, last_pushed_seq) VALUES (1, NULL, 0);
"""


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Writers queue rather than failing instantly when another thread holds the
    # write lock. WAL allows concurrent readers alongside one writer.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class Database:
    """One sqlite connection per thread, against one file.

    FastAPI runs sync endpoints in a threadpool while the tick loop runs on the
    event loop, so the connection is genuinely reached from several threads.
    `check_same_thread=False` permits that but does not make it safe - sharing
    one connection concurrently raises "bad parameter or other API misuse",
    which is exactly what it did.

    Per-thread connections sidestep it entirely, and WAL means readers never
    block the writer.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Create the schema once, up front, on whichever thread builds this.
        self._connection().executescript(SCHEMA)

    def _connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = _open(self.path)
            self._local.conn = conn
        return conn

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self._connection().execute(sql, params)

    def executescript(self, script: str) -> sqlite3.Cursor:
        return self._connection().executescript(script)

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def connect(path: Path) -> Database:
    return Database(path)
