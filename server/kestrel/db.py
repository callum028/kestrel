from __future__ import annotations

import sqlite3
from pathlib import Path

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
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
