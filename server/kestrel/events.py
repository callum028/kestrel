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

from .db import Database


class EventKind(StrEnum):
    # Task lifecycle
    TASK_CREATED = "task.created"
    TASK_BRIEFED = "task.briefed"
    TASK_STATE_CHANGED = "task.state_changed"
    TASK_PROGRESS = "task.progress"
    TASK_QUESTION = "task.question"
    TASK_ANSWERED = "task.answered"
    TASK_CLOSED = "task.closed"
    TASK_TICKET_LINKED = "task.ticket_linked"

    # Board (Notion). Mechanical - a deterministic side-effect of task state,
    # never a model decision; Claude never touches the board. Every write is
    # logged so drift is debuggable and so the poller can tell its own echo
    # apart from a manual move.
    BOARD_WRITTEN = "board.written"
    BOARD_INSTRUCTION = "board.instruction"

    # Session activity. Note this is activity, not progress - the distinction is
    # the whole point of the stall detector.
    TOOL_CALL = "session.tool_call"
    PROMPT_SUBMITTED = "session.prompt_submitted"  # UserPromptSubmit - also
    # proof the session got past any startup dialog, for the startup-stall
    # detector.

    # Executor lifecycle - a session starting or ending, distinct from the
    # task state machine in tasks.py (an executor can be restarted without
    # the task itself changing state).
    SESSION_STARTED = "executor.session_started"
    SESSION_STOPPED = "executor.session_stopped"
    SEND_DEFERRED = "executor.send_deferred"

    # Supervision
    STALL_DETECTED = "supervision.stall_detected"
    NUDGE_SENT = "supervision.nudge_sent"
    NUDGE_HELD_BACK = "supervision.nudge_held_back"
    CLAIM_REJECTED = "supervision.claim_rejected"
    CLAIM_ACCEPTED = "supervision.claim_accepted"
    VALIDATION_RUN = "supervision.validation_run"

    # Kestrel-owned waits - see waits.py. Claude registers one instead of
    # arming its own watcher; Kestrel polls the real thing and wakes the
    # session, or escalates on deadline expiry.
    WAIT_REGISTERED = "wait.registered"
    WAIT_RESOLVED = "wait.resolved"
    WAIT_EXPIRED = "wait.expired"

    # GitHub - PR discovery, CI status, merge-on-green. Mechanical, same as
    # the board: a deterministic side-effect of what the tick observes, never
    # a model decision.
    PR_DETECTED = "github.pr_detected"
    CI_CHECKED = "github.ci_checked"
    PR_MERGED = "github.pr_merged"

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
    PUSH_SENT = "channel.push_sent"
    PUSH_FAILED = "channel.push_failed"
    PUSH_SUBSCRIBED = "channel.push_subscribed"
    PUSH_UNSUBSCRIBED = "channel.push_unsubscribed"

    # Conversation - the one Kestrel chat, shared across every device.
    CONVERSATION_MESSAGE = "conversation.message"

    # Mail - read-only, only on request. Every read is recorded here (what was
    # read, never the body), which is the audit trail for a capability whose
    # whole risk surface is "what did Kestrel look at".
    MAIL_LISTED = "mail.listed"
    MAIL_READ = "mail.read"

    # System. A failing tick is recorded rather than swallowed - an assistant
    # that dies quietly is worse than one that reports a bad pass.
    TICK_FAILED = "system.tick_failed"
    SESSION_BOUND = "system.session_bound"
    HOOK_UNATTRIBUTED = "system.hook_unattributed"

    # Restart reconciliation - a task believed running is checked against the
    # session host on startup; anything unaccountable is reported, never
    # assumed healthy.
    RECONCILE_UNACCOUNTABLE = "system.reconcile_unaccountable"


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

    def __init__(self, conn: Database) -> None:
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
