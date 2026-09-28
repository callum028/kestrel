"""The Kestrel chat - one conversation, shared across every device.

Distinct from a Claude chat: this is Kestrel speaking, not a session's raw
terminal output. There is exactly one of these (no per-client history, no
per-task history - `refs` is how a message points *at* a task without the
conversation being split by one), which is why the store takes no conversation
id anywhere in its API.

**Contract for the brain step (later):** implement `Responder.respond` against
a real model. It receives the new user text and the recent history and returns
the reply text - nothing else in this module needs to change. `post_user_message`
already does the plumbing: store the user message, ask the responder, store the
reply, log both. Swapping `StubResponder` for a real one is the entire brain
integration as far as this module is concerned; retrieval, tool calls and
context assembly (docs/design.md §4.5) live inside the responder, not here.

Message shape returned to clients:
    {id, role: "user" | "kestrel" | "system", text, created_at, refs: [{kind, handle}]}

`refs` lets a message point at a task (`{"kind": "task", "handle": "KES-31"}`)
without conversation and tasks becoming the same table - the task detail view
can pull "what Kestrel said about this" by filtering on it client-side, or a
future endpoint can do it server-side without a schema change.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .db import Database
from .events import EventKind, EventLog

BRAIN_NOT_CONNECTED = "Kestrel's brain isn't connected yet."


@dataclass(frozen=True)
class Ref:
    kind: str
    handle: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "handle": self.handle}


@dataclass(frozen=True)
class ConversationMessage:
    id: int
    role: str
    text: str
    created_at: datetime
    refs: list[Ref] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "created_at": self.created_at.isoformat(),
            "refs": [r.to_dict() for r in self.refs],
        }


class Responder(Protocol):
    """One method, implemented against a real model in the brain step. Takes
    the new message and the conversation so far; returns Kestrel's reply text.
    Everything about *how* that reply is produced - context assembly, retrieval,
    tool calls - is the responder's problem, not the conversation store's."""

    async def respond(self, text: str, history: list[ConversationMessage]) -> str: ...


class StubResponder:
    """So the UI is testable end to end before there is a brain to talk to.
    Every reply is the same fixed sentence - real, not mocked, just not smart."""

    name = "stub"

    async def respond(self, text: str, history: list[ConversationMessage]) -> str:
        return BRAIN_NOT_CONNECTED


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_message(row: sqlite3.Row) -> ConversationMessage:
    return ConversationMessage(
        id=row["id"],
        role=row["role"],
        text=row["text"],
        created_at=datetime.fromisoformat(row["created_at"]),
        refs=[Ref(**r) for r in json.loads(row["refs"])],
    )


class ConversationStore:
    def __init__(self, conn: Database, log: EventLog, responder: Responder | None = None) -> None:
        self._conn = conn
        self._log = log
        self._responder = responder or StubResponder()

    def _insert(
        self,
        role: str,
        text: str,
        refs: list[Ref] | None = None,
        now: datetime | None = None,
    ) -> ConversationMessage:
        now = now or datetime.now(UTC)
        cur = self._conn.execute(
            "INSERT INTO conversation_messages (role, text, created_at, refs) VALUES (?, ?, ?, ?)",
            (role, text, now.isoformat(), json.dumps([r.to_dict() for r in refs or []])),
        )
        row = self._conn.execute(
            "SELECT * FROM conversation_messages WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        message = _to_message(row)
        self._log.append(EventKind.CONVERSATION_MESSAGE, role, {"id": message.id, "role": role})
        return message

    async def post_user_message(
        self, text: str, refs: list[Ref] | None = None, now: datetime | None = None
    ) -> ConversationMessage:
        """Stores the user's message, then asks the responder for Kestrel's
        reply and stores that too - both land in the same table, one after the
        other, so a client polling `since` sees the full exchange. Returns only
        the user message, per the endpoint contract; the reply shows up on the
        next poll like anything else Kestrel says."""
        user_message = self._insert("user", text, refs, now)
        history = self.since(limit=50)
        reply_text = await self._responder.respond(text, history)
        self._insert("kestrel", reply_text)
        return user_message

    def since(self, after: int = 0, limit: int = 200) -> list[ConversationMessage]:
        """Oldest to newest, unlike events.since which this otherwise mirrors -
        a chat reads top-to-bottom, not most-recent-first."""
        rows = self._conn.execute(
            "SELECT * FROM conversation_messages WHERE id > ? ORDER BY id LIMIT ?",
            (after, limit),
        ).fetchall()
        return [_to_message(r) for r in rows]
