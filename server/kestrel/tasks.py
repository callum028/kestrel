"""Tasks - the generic unit of delegated work.

Deliberately not Claude-Code-shaped. A task is a goal, acceptance criteria, scope
bounds and a pointer to an executor; the lifecycle, escalation and record-writing
live here and are written once. Code supervision is one executor among four.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from .db import Database
from .events import EventKind, EventLog


class TaskState(StrEnum):
    CREATED = "created"
    BRIEFED = "briefed"
    RUNNING = "running"
    NEEDS_INPUT = "needs_input"
    AWAITING_DEV = "awaiting_dev"  # queued on the dev-environment lock
    VALIDATING = "validating"
    PARKED = "parked"
    DONE = "done"
    FAILED = "failed"


# A task waiting on the dev lock is not blocked and does not escalate - it queues.
LEGAL: dict[TaskState, set[TaskState]] = {
    TaskState.CREATED: {TaskState.BRIEFED, TaskState.FAILED},
    TaskState.BRIEFED: {TaskState.RUNNING, TaskState.PARKED, TaskState.FAILED},
    TaskState.RUNNING: {
        TaskState.NEEDS_INPUT,
        TaskState.AWAITING_DEV,
        TaskState.PARKED,
        TaskState.FAILED,
    },
    TaskState.NEEDS_INPUT: {TaskState.RUNNING, TaskState.PARKED, TaskState.FAILED},
    TaskState.AWAITING_DEV: {TaskState.VALIDATING, TaskState.PARKED, TaskState.FAILED},
    # Failed validation reopens the task with evidence; it does not revert.
    TaskState.VALIDATING: {TaskState.DONE, TaskState.RUNNING, TaskState.PARKED, TaskState.FAILED},
    TaskState.PARKED: {TaskState.RUNNING, TaskState.FAILED},
    TaskState.DONE: set(),
    TaskState.FAILED: set(),
}


class IllegalTransition(Exception):
    pass


@dataclass
class Task:
    id: str
    handle: str
    goal: str
    criteria: list[str]
    executor: str
    state: TaskState
    scope: str | None = None
    ticket_ref: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_progress_at: datetime | None = None
    progress_hash: str | None = None
    nudges: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def _to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        handle=row["handle"],
        goal=row["goal"],
        criteria=json.loads(row["criteria"]),
        executor=row["executor"],
        state=TaskState(row["state"]),
        scope=row["scope"],
        ticket_ref=row["ticket_ref"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_progress_at=_parse(row["last_progress_at"]),
        progress_hash=row["progress_hash"],
        nudges=row["nudges"],
    )


class TaskStore:
    """Projection over the event log. Every mutation appends an event first."""

    def __init__(self, conn: Database, log: EventLog) -> None:
        self._conn = conn
        self._log = log

    def create(
        self,
        handle: str,
        goal: str,
        criteria: list[str],
        executor: str,
        scope: str | None = None,
        ticket_ref: str | None = None,
        actor: str = "kestrel",
    ) -> Task:
        task_id = str(uuid.uuid4())
        now = _now()
        self._log.append(
            EventKind.TASK_CREATED,
            actor,
            {"handle": handle, "goal": goal, "criteria": criteria, "executor": executor},
            task_id=task_id,
        )
        self._conn.execute(
            """INSERT INTO tasks
               (id, handle, goal, criteria, scope, executor, state, ticket_ref,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                handle,
                goal,
                json.dumps(criteria),
                scope,
                executor,
                str(TaskState.CREATED),
                ticket_ref,
                now,
                now,
            ),
        )
        return self.get(task_id)

    def get(self, task_id: str) -> Task:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return _to_task(row)

    def by_handle(self, handle: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE handle = ?", (handle,)).fetchone()
        return _to_task(row) if row else None

    def by_ticket_ref(self, ticket_ref: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE ticket_ref = ?", (ticket_ref,)
        ).fetchone()
        return _to_task(row) if row else None

    def set_ticket_ref(self, task_id: str, ticket_ref: str, actor: str = "kestrel") -> Task:
        """Anything asked of Kestrel that isn't already a ticket gets one created
        first - this records the link once that ticket exists."""
        self._log.append(
            EventKind.TASK_TICKET_LINKED, actor, {"ticket_ref": ticket_ref}, task_id=task_id
        )
        self._conn.execute(
            "UPDATE tasks SET ticket_ref = ?, updated_at = ? WHERE id = ?",
            (ticket_ref, _now(), task_id),
        )
        return self.get(task_id)

    def active(self) -> list[Task]:
        terminal = (str(TaskState.DONE), str(TaskState.FAILED))
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE state NOT IN (?, ?) ORDER BY created_at", terminal
        ).fetchall()
        return [_to_task(r) for r in rows]

    def transition(
        self, task_id: str, to: TaskState, actor: str = "kestrel", **why: object
    ) -> Task:
        task = self.get(task_id)
        if to not in LEGAL[task.state]:
            raise IllegalTransition(f"{task.handle}: {task.state} -> {to}")
        self._log.append(
            EventKind.TASK_STATE_CHANGED,
            actor,
            {"from": str(task.state), "to": str(to), **why},
            task_id=task_id,
        )
        self._conn.execute(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?", (str(to), _now(), task_id)
        )
        return self.get(task_id)

    def record_progress(self, task_id: str, progress_hash: str, actor: str = "agent") -> bool:
        """Returns True if this represents real progress.

        Activity is not progress - a polling loop produces plenty of activity. The
        worktree diff hash is the proxy that distinguishes them.
        """
        task = self.get(task_id)
        moved = progress_hash != task.progress_hash
        if moved:
            self._log.append(
                EventKind.TASK_PROGRESS, actor, {"hash": progress_hash}, task_id=task_id
            )
            self._conn.execute(
                "UPDATE tasks SET progress_hash = ?, last_progress_at = ?, nudges = 0 WHERE id = ?",
                (progress_hash, _now(), task_id),
            )
        return moved

    def record_nudge(self, task_id: str, evidence: str, actor: str = "kestrel") -> int:
        self._log.append(EventKind.NUDGE_SENT, actor, {"evidence": evidence}, task_id=task_id)
        self._conn.execute("UPDATE tasks SET nudges = nudges + 1 WHERE id = ?", (task_id,))
        return self.get(task_id).nudges
