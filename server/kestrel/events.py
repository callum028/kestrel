"""The event log - the spine.

Everything is already events: hooks, focus changes, presence, task transitions,
CI results, nudges. All other state is a projection of this table, which is what
makes replay, debugging and "what happened yesterday" free rather than features.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    # Task lifecycle
    TASK_CREATED = "task.created"
    TASK_BRIEFED = "task.briefed"
    TASK_STATE_CHANGED = "task.state_changed"
    TASK_PROGRESS = "task.progress"
    TASK_QUESTION = "task.question"
    TASK_ANSWERED = "task.answered"
    TASK_CLOSED = "task.closed"

    # Session activity. Note this is activity, not progress - the distinction is
    # the whole point of the stall detector.
    TOOL_CALL = "session.tool_call"

    # Supervision
    STALL_DETECTED = "supervision.stall_detected"
    NUDGE_SENT = "supervision.nudge_sent"
    CLAIM_REJECTED = "supervision.claim_rejected"
    VALIDATION_RUN = "supervision.validation_run"

    # Observations
    OBSERVATION_RAISED = "observation.raised"
    OBSERVATION_RESOLVED = "observation.resolved"

    # Attention
    PRESENCE_CHANGED = "attention.presence_changed"
    FOCUS_CHANGED = "attention.focus_changed"

    # Memory - every durable write has a visible moment of creation
    MEMORY_WRITTEN = "memory.written"
    MEMORY_SUPERSEDED = "memory.superseded"

    # Delivery
    MESSAGE_DELIVERED = "channel.delivered"
    MESSAGE_ACKNOWLEDGED = "channel.acknowledged"

    # System. A failing tick is recorded rather than swallowed - an assistant
    # that dies quietly is worse than one that reports a bad pass.
    TICK_FAILED = "system.tick_failed"
    SESSION_BOUND = "system.session_bound"
    HOOK_UNATTRIBUTED = "system.hook_unattributed"


@dataclass(frozen=True)
class Event:
    seq: int
    ts: datetime
    kind: EventKind
    actor: str
    task_id: str | None
    payload: dict[str, Any]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_event(row: sqlite3.Row) -> Event:
    return Event(
        seq=row["seq"],
        ts=datetime.fromisoformat(row["ts"]),
        kind=EventKind(row["kind"]),
        actor=row["actor"],
        task_id=row["task_id"],
        payload=json.loads(row["payload"]),
    )


class EventLog:
    """Append-only. The only write path into durable state."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(
        self,
        kind: EventKind,
        actor: str,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> Event:
        cur = self._conn.execute(
            "INSERT INTO events (ts, kind, actor, task_id, payload) VALUES (?, ?, ?, ?, ?)",
            (_now(), str(kind), actor, task_id, json.dumps(payload or {})),
        )
        row = self._conn.execute("SELECT * FROM events WHERE seq = ?", (cur.lastrowid,)).fetchone()
        return _to_event(row)

    def since(self, seq: int = 0, limit: int = 500) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)
        ).fetchall()
        return [_to_event(r) for r in rows]

    def for_task(self, task_id: str) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE task_id = ? ORDER BY seq", (task_id,)
        ).fetchall()
        return [_to_event(r) for r in rows]

    def of_kind(self, kind: EventKind, limit: int = 100) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE kind = ? ORDER BY seq DESC LIMIT ?", (str(kind), limit)
        ).fetchall()
        return [_to_event(r) for r in rows]

    def replay(self) -> Iterator[Event]:
        for row in self._conn.execute("SELECT * FROM events ORDER BY seq"):
            yield _to_event(row)
