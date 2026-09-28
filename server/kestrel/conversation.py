"""The Kestrel chat - one conversation, shared across every device.

Distinct from a Claude chat: this is Kestrel speaking, not a session's raw
terminal output. There is exactly one of these (no per-client history, no
per-task history - `refs` is how a message points *at* a task without the
conversation being split by one), which is why the store takes no conversation
id anywhere in its API.

**Brain step:** `Responder.respond` is implemented against a real model in
`kestrel.brain.responder.BrainResponder` - it receives the new user text and
the recent history and returns the reply text; retrieval, tool calls and
context assembly (docs/design.md §4.5) live inside it, not here.
`post_user_message` stores the user message and returns immediately; the
reply is generated in the background (a headless `claude -p` call is not
something a request handler should block on) and lands in this same table
once it's ready - see `_respond_and_store` and the `thinking` property.

Message shape returned to clients:
    {id, role: "user" | "kestrel" | "system", text, created_at, refs: [{kind, handle}]}

`refs` lets a message point at a task (`{"kind": "task", "handle": "KES-31"}`)
without conversation and tasks becoming the same table - the task detail view
can pull "what Kestrel said about this" by filtering on it client-side, or a
future endpoint can do it server-side without a schema change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .db import Database
from .events import EventKind, EventLog

logger = logging.getLogger("kestrel.conversation")

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
        # Background reply tasks in flight. A set, not a single slot, because
        # nothing stops Callum sending a second message before the first
        # reply lands - both run, both land in order they finish, same as
        # any other "no fan-out, but never block on one thing" surface here.
        self._pending: set[asyncio.Task[None]] = set()

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
        """Stores the user's message and returns immediately - the reply is
        generated in the background (the brain is a subprocess call, and
        posting a message must not block on it) and lands via the same table
        once it's ready, so a client polling `since` sees it appear like
        anything else Kestrel says. `thinking` is true for the window in
        between, for a client that wants to show that.

        A responder that raises is not swallowed: it becomes a visible
        system-role message plus a logged `BRAIN_CALL_FAILED` event, per
        "every instruction ends in a state" - silence is never an outcome,
        including when the brain itself is the thing that broke.
        """
        user_message = self._insert("user", text, refs, now)
        history = self.since(limit=50)
        task = asyncio.create_task(self._respond_and_store(text, history))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return user_message

    async def _respond_and_store(self, text: str, history: list[ConversationMessage]) -> None:
        try:
            reply_text = await self._responder.respond(text, history)
        except Exception as exc:
            logger.exception("brain responder failed")
            self._log.append(EventKind.BRAIN_CALL_FAILED, "kestrel", {"error": str(exc)})
            self._insert("system", f"Kestrel's brain didn't respond: {exc}")
            return
        self._insert("kestrel", reply_text)

    def set_responder(self, responder: Responder) -> None:
        """Swap the responder after construction. `Runtime.build` uses this
        to wire in a real brain once the rest of `Runtime` exists - the brain
        needs `rt.state_block`/`rt.tasks.active`, neither of which exist yet
        at the point this store itself is built."""
        self._responder = responder

    @property
    def thinking(self) -> bool:
        """True while at least one reply is being generated in the
        background - the visible "thinking" state the brain step asks for."""
        return bool(self._pending)

    async def wait_idle(self) -> None:
        """Test/debug helper: block until every in-flight reply has landed.
        Never called from request-handling code - that is the entire point
        of making replies asynchronous."""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    def since(self, after: int = 0, limit: int = 200) -> list[ConversationMessage]:
        """Oldest to newest, unlike events.since which this otherwise mirrors -
        a chat reads top-to-bottom, not most-recent-first."""
        rows = self._conn.execute(
            "SELECT * FROM conversation_messages WHERE id > ? ORDER BY id LIMIT ?",
            (after, limit),
        ).fetchall()
        return [_to_message(r) for r in rows]
