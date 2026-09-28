"""Kestrel-owned waits.

Claude must not arm its own watchers - `gh run watch` in a loop, a sleep in a
shell, polling a health endpoint from inside the session. That is exactly the
overnight failure the rest of supervision exists to catch, and asking the
model to poll responsibly does not fix it; not polling at all does. Instead a
task registers "waiting on X" with a deadline (via the `kestrel-wait` CLI, see
`kestrel_agent.kestrel_wait`, POSTing to `/waits`) and ends its turn. Kestrel
polls the real thing once per tick and, when it resolves or the deadline
passes, wakes the session with a concise factual message - see
`orchestrator.py`'s wait-checking pass.

This module only owns the storage and the pure "is it due" arithmetic. What a
wait resolves *against* (a GitHub CI run, a URL, nothing but the clock) is the
orchestrator's job, because that is where the GitHub client and the HTTP
checker already live.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .db import Database
from .events import EventKind, EventLog

# How long a wait may sit unresolved before Kestrel checks once more and, if
# still unresolved, escalates instead of continuing to poll forever. Callers
# may pass an explicit deadline (e.g. `kestrel-wait ci --timeout 45`); this is
# only the default when none is given.
DEFAULT_TIMEOUT_MINUTES: dict[str, float] = {
    "ci": 45.0,
    "url": 20.0,
    "deadline": 60.0,
}


class WaitKind(StrEnum):
    CI = "ci"
    URL = "url"
    DEADLINE = "deadline"


@dataclass(frozen=True)
class Wait:
    id: str
    task_id: str
    kind: WaitKind
    params: dict[str, Any]
    deadline: datetime
    created_at: datetime
    resolved_at: datetime | None = None
    result: str | None = None

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None

    def due(self, now: datetime) -> bool:
        return not self.resolved and now >= self.deadline


def _to_wait(row: sqlite3.Row) -> Wait:
    return Wait(
        id=row["id"],
        task_id=row["task_id"],
        kind=WaitKind(row["kind"]),
        params=json.loads(row["params"]),
        deadline=datetime.fromisoformat(row["deadline"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
        result=row["result"],
    )


class WaitStore:
    def __init__(self, conn: Database, log: EventLog) -> None:
        self._conn = conn
        self._log = log

    def register(
        self,
        task_id: str,
        kind: WaitKind,
        params: dict[str, Any],
        deadline: datetime,
        actor: str = "agent",
    ) -> Wait:
        wait_id = uuid.uuid4().hex[:12]
        now = datetime.now(UTC)
        self._conn.execute(
            """INSERT INTO waits (id, task_id, kind, params, deadline, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                wait_id,
                task_id,
                str(kind),
                json.dumps(params),
                deadline.isoformat(),
                now.isoformat(),
            ),
        )
        self._log.append(
            EventKind.WAIT_REGISTERED,
            actor,
            {
                "wait_id": wait_id,
                "kind": str(kind),
                "params": params,
                "deadline": deadline.isoformat(),
            },
            task_id=task_id,
        )
        return self.get(wait_id)

    def get(self, wait_id: str) -> Wait:
        row = self._conn.execute("SELECT * FROM waits WHERE id = ?", (wait_id,)).fetchone()
        if row is None:
            raise KeyError(wait_id)
        return _to_wait(row)

    def active_for_task(self, task_id: str) -> list[Wait]:
        rows = self._conn.execute(
            "SELECT * FROM waits WHERE task_id = ? AND resolved_at IS NULL ORDER BY created_at",
            (task_id,),
        ).fetchall()
        return [_to_wait(r) for r in rows]

    def active(self) -> list[Wait]:
        rows = self._conn.execute(
            "SELECT * FROM waits WHERE resolved_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [_to_wait(r) for r in rows]

    def resolve(self, wait_id: str, result: str, actor: str = "kestrel") -> Wait:
        now = datetime.now(UTC)
        wait = self.get(wait_id)
        self._conn.execute(
            "UPDATE waits SET resolved_at = ?, result = ? WHERE id = ?",
            (now.isoformat(), result, wait_id),
        )
        self._log.append(
            EventKind.WAIT_RESOLVED,
            actor,
            {"wait_id": wait_id, "result": result},
            task_id=wait.task_id,
        )
        return self.get(wait_id)

    def expire(self, wait_id: str, result: str, actor: str = "kestrel") -> Wait:
        """Same bookkeeping as `resolve`, logged distinctly - a deadline that
        passed with the underlying thing still unresolved is a different fact
        than the thing resolving, and the morning report should be able to
        tell them apart."""
        now = datetime.now(UTC)
        wait = self.get(wait_id)
        self._conn.execute(
            "UPDATE waits SET resolved_at = ?, result = ? WHERE id = ?",
            (now.isoformat(), result, wait_id),
        )
        self._log.append(
            EventKind.WAIT_EXPIRED,
            actor,
            {"wait_id": wait_id, "result": result},
            task_id=wait.task_id,
        )
        return self.get(wait_id)


def default_deadline(kind: WaitKind, now: datetime, timeout_minutes: float | None) -> datetime:
    minutes = timeout_minutes if timeout_minutes is not None else DEFAULT_TIMEOUT_MINUTES[str(kind)]
    return now + timedelta(minutes=minutes)
