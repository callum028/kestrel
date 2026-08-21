"""Delivery - saying something to Callum, once.

Two rules, both learned the hard way from assistants that get muted:

- **Escalate on silence, never fan out.** Sending to desktop and phone together
  means dismissing everything twice, and within a week both get ignored. Send to
  the best channel for where his attention is, and climb a rung if it goes
  unacknowledged.
- **Acknowledging anywhere dismisses everywhere.** The Pi owns the state, so a
  notification answered at the desk is cleared on the phone too.

Content differs by channel rather than only rendering: voice gets two sentences,
the desktop gets the full text at the same moment. Nothing is lost by being away.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .attention import AttentionState, Channel, Urgency, choose_channel, escalate
from .db import Database
from .events import EventKind, EventLog

ESCALATE_AFTER = timedelta(minutes=2)
VOICE_SENTENCE_LIMIT = 2


@dataclass(frozen=True)
class Delivery:
    id: str
    subject: str
    body: str
    urgency: Urgency
    channel: Channel
    task_id: str | None
    sent_at: datetime
    acked_at: datetime | None
    acked_on: str | None
    escalations: int

    @property
    def acknowledged(self) -> bool:
        return self.acked_at is not None


def for_voice(body: str) -> str:
    """Two sentences and stop. The full version is already on the desktop, so
    truncating here loses nothing - and reading a stack trace aloud is useless."""
    sentences = [s.strip() for s in body.replace("\n", " ").split(". ") if s.strip()]
    clipped = ". ".join(sentences[:VOICE_SENTENCE_LIMIT])
    return clipped if clipped.endswith((".", "?", "!")) else f"{clipped}."


class DeliveryTracker:
    def __init__(self, conn: Database, log: EventLog) -> None:
        self._conn = conn
        self._log = log

    def send(
        self,
        subject: str,
        body: str,
        urgency: Urgency,
        state: AttentionState,
        task_id: str | None = None,
        about_task: str | None = None,
        now: datetime | None = None,
    ) -> Delivery:
        now = now or datetime.now(UTC)
        channel = choose_channel(state, urgency, focused_on=about_task)
        delivery = Delivery(
            id=uuid.uuid4().hex[:12],
            subject=subject,
            body=body,
            urgency=urgency,
            channel=channel,
            task_id=task_id,
            sent_at=now,
            acked_at=None,
            acked_on=None,
            escalations=0,
        )
        self._conn.execute(
            """INSERT INTO deliveries
               (id, subject, body, urgency, channel, task_id, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                delivery.id,
                subject,
                body,
                str(urgency),
                str(channel),
                task_id,
                now.isoformat(),
            ),
        )
        self._log.append(
            EventKind.MESSAGE_DELIVERED,
            "kestrel",
            {"id": delivery.id, "channel": str(channel), "urgency": str(urgency)},
            task_id=task_id,
        )
        return delivery

    def acknowledge(self, delivery_id: str, on: str, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self._conn.execute(
            "UPDATE deliveries SET acked_at = ?, acked_on = ? WHERE id = ? AND acked_at IS NULL",
            (now.isoformat(), on, delivery_id),
        )
        self._log.append(EventKind.MESSAGE_ACKNOWLEDGED, "callum", {"id": delivery_id, "on": on})

    def get(self, delivery_id: str) -> Delivery:
        row = self._conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return self._to_delivery(row)

    def pending(self) -> list[Delivery]:
        rows = self._conn.execute(
            "SELECT * FROM deliveries WHERE acked_at IS NULL ORDER BY sent_at"
        ).fetchall()
        return [self._to_delivery(r) for r in rows]

    def due_for_escalation(
        self, now: datetime, after: timedelta = ESCALATE_AFTER
    ) -> list[Delivery]:
        return [d for d in self.pending() if now - d.sent_at >= after]

    def escalate(self, delivery_id: str, now: datetime | None = None) -> Delivery | None:
        """Returns None when the ceiling for this urgency has been reached.

        An observation never climbs past a notification, so a low-value finding
        cannot end up ringing him however long it goes unread.
        """
        now = now or datetime.now(UTC)
        current = self.get(delivery_id)
        nxt = escalate(current.channel, current.urgency)
        if nxt is None:
            return None
        self._conn.execute(
            """UPDATE deliveries
               SET channel = ?, sent_at = ?, escalations = escalations + 1
               WHERE id = ?""",
            (str(nxt), now.isoformat(), delivery_id),
        )
        self._log.append(
            EventKind.MESSAGE_DELIVERED,
            "kestrel",
            {"id": delivery_id, "channel": str(nxt), "escalated": True},
            task_id=current.task_id,
        )
        return self.get(delivery_id)

    @staticmethod
    def _to_delivery(row: sqlite3.Row) -> Delivery:
        return Delivery(
            id=row["id"],
            subject=row["subject"],
            body=row["body"],
            urgency=Urgency(row["urgency"]),
            channel=Channel(row["channel"]),
            task_id=row["task_id"],
            sent_at=datetime.fromisoformat(row["sent_at"]),
            acked_at=datetime.fromisoformat(row["acked_at"]) if row["acked_at"] else None,
            acked_on=row["acked_on"],
            escalations=row["escalations"],
        )
