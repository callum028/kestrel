"""The dev-environment lock.

There is no localhost, so merging and deploying to dev is how work becomes
testable at all. That makes dev a shared singleton, and therefore a lock rather
than a step: two tasks cannot validate at once, because the second would be
testing the first one's code.

Consequences, all of which fall out of holding the lock from merge until
validation completes:

- tasks run concurrently in isolated worktrees but serialise here
- a task waiting on the lock is not blocked and does not escalate; it queues
- anything still in flight rebases and revalidates after each landing
- a stuck deploy holds the lock and stalls everything behind it, so the hold
  itself needs a timeout and is worth reporting

This, not subscription rate limits, is the real cap on useful concurrency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .db import Database

STUCK_AFTER = timedelta(minutes=25)


@dataclass(frozen=True)
class Hold:
    task_id: str
    acquired_at: datetime

    def held_for(self, now: datetime) -> timedelta:
        return now - self.acquired_at

    def is_stuck(self, now: datetime, after: timedelta = STUCK_AFTER) -> bool:
        return self.held_for(now) > after


class DevLock:
    def __init__(self, conn: Database) -> None:
        self._conn = conn

    def holder(self) -> Hold | None:
        row = self._conn.execute(
            "SELECT task_id, acquired_at FROM dev_lock WHERE id = 1"
        ).fetchone()
        if row is None or row["task_id"] is None:
            return None
        return Hold(task_id=row["task_id"], acquired_at=datetime.fromisoformat(row["acquired_at"]))

    def acquire(self, task_id: str, now: datetime | None = None) -> bool:
        """True if the lock is now held by this task, False if it queued.

        Queuing is a normal outcome, not a failure - which is why this does not
        raise and does not produce an escalation.
        """
        now = now or datetime.now(UTC)
        held = self.holder()
        if held is not None and held.task_id != task_id:
            self._conn.execute(
                "INSERT OR IGNORE INTO dev_lock_queue (task_id, queued_at) VALUES (?, ?)",
                (task_id, now.isoformat()),
            )
            return False
        self._conn.execute(
            "UPDATE dev_lock SET task_id = ?, acquired_at = ? WHERE id = 1",
            (task_id, now.isoformat()),
        )
        self._conn.execute("DELETE FROM dev_lock_queue WHERE task_id = ?", (task_id,))
        return True

    def release(self, task_id: str) -> str | None:
        """Releases and returns the next task in the queue, if any."""
        held = self.holder()
        if held is None or held.task_id != task_id:
            return None
        self._conn.execute("UPDATE dev_lock SET task_id = NULL, acquired_at = NULL WHERE id = 1")
        row = self._conn.execute(
            "SELECT task_id FROM dev_lock_queue ORDER BY queued_at LIMIT 1"
        ).fetchone()
        return row["task_id"] if row else None

    def queue(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT task_id FROM dev_lock_queue ORDER BY queued_at"
        ).fetchall()
        return [r["task_id"] for r in rows]

    def stuck(self, now: datetime | None = None, after: timedelta = STUCK_AFTER) -> Hold | None:
        now = now or datetime.now(UTC)
        held = self.holder()
        return held if held and held.is_stuck(now, after) else None
