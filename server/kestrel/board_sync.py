"""The board sync worker.

Two halves, running from the same tick:

- **Push.** Task state transitions, read off the event log, become status/flag/
  PR/comment writes on the board. Mechanical - no model involved, which is why
  it can safely run unattended. Claude never touches the board; this is the
  only thing that does.
- **Poll.** Tickets changed on the board since the last poll are read back. If
  Callum dragged a card - to Done, or back to To Do - that counts as an
  instruction and gets turned into a task-state transition. If what changed is
  Kestrel's own last push reflected back, it is dropped: `board_sync` tracks
  the lane *Kestrel itself last wrote* per ticket, so the echo is recognised
  before it gets to `tasks.transition`, rather than by comparing timestamps or
  hoping the poll runs slower than the write.

Every board write is recorded as an event (`EventKind.BOARD_WRITTEN`), and every
manual move recognised as an instruction is too (`BOARD_INSTRUCTION`) - both are
mechanical facts, not supervision judgement, so they do not go through the
orchestrator's ladder.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .board import DEFAULT_FLAG_STATES, DEFAULT_LANE_MAP, Board, Ticket
from .db import Database
from .events import EventKind, EventLog
from .tasks import IllegalTransition, TaskState, TaskStore

logger = logging.getLogger("kestrel.board_sync")

# Events that change what should be on the board. Everything else advances the
# push cursor without producing a write.
_PUSH_KINDS = {EventKind.TASK_CREATED, EventKind.TASK_STATE_CHANGED}


class BoardSync:
    def __init__(
        self,
        board: Board,
        tasks: TaskStore,
        log: EventLog,
        conn: Database,
        lane_map: dict[TaskState, str] | None = None,
        flag_states: frozenset[TaskState] | None = None,
    ) -> None:
        self._board = board
        self._tasks = tasks
        self._log = log
        self._conn = conn
        self._lane_map = lane_map or DEFAULT_LANE_MAP
        self._flag_states = flag_states or DEFAULT_FLAG_STATES

    # -- cursor -----------------------------------------------------------

    def _cursor(self) -> tuple[datetime | None, int]:
        row = self._conn.execute(
            "SELECT last_poll_at, last_pushed_seq FROM board_cursor WHERE id = 1"
        ).fetchone()
        last_poll = datetime.fromisoformat(row["last_poll_at"]) if row["last_poll_at"] else None
        return last_poll, row["last_pushed_seq"]

    def _save_pushed_seq(self, seq: int) -> None:
        self._conn.execute("UPDATE board_cursor SET last_pushed_seq = ? WHERE id = 1", (seq,))

    def _save_poll_cursor(self, when: datetime) -> None:
        self._conn.execute(
            "UPDATE board_cursor SET last_poll_at = ? WHERE id = 1", (when.isoformat(),)
        )

    def _sync_row(self, ticket_id: str) -> tuple[str | None, bool] | None:
        row = self._conn.execute(
            "SELECT last_pushed_lane, last_pushed_flag FROM board_sync WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchone()
        if row is None:
            return None
        return row["last_pushed_lane"], bool(row["last_pushed_flag"])

    def _record_push(self, ticket_id: str, task_id: str, lane: str, flagged: bool) -> None:
        self._conn.execute(
            """INSERT INTO board_sync (ticket_id, task_id, last_pushed_lane, last_pushed_flag, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ticket_id) DO UPDATE SET
                 last_pushed_lane = excluded.last_pushed_lane,
                 last_pushed_flag = excluded.last_pushed_flag,
                 updated_at = excluded.updated_at""",
            (ticket_id, task_id, lane, int(flagged), datetime.now(UTC).isoformat()),
        )

    # -- push: task state -> board -----------------------------------------

    async def push(self) -> int:
        """Mirrors task lifecycle events onto the board. Returns the number of
        board writes made."""
        _, last_seq = self._cursor()
        events = self._log.since(last_seq)
        if not events:
            return 0

        writes = 0
        for event in events:
            if event.kind in _PUSH_KINDS and event.task_id:
                writes += await self._push_task(event.task_id, event.kind, event.payload)
            self._save_pushed_seq(event.seq)
        return writes

    async def _push_task(self, task_id: str, kind: EventKind, payload: dict) -> int:
        task = self._tasks.get(task_id)
        writes = 0

        if task.ticket_ref is None:
            # Anything asked of Kestrel that is not already a ticket gets one
            # created first - this is that creation, driven by the task
            # existing rather than by whoever asked for it.
            lane = self._lane_map.get(task.state, "To Do")
            ticket = await self._board.create(
                title=task.goal,
                body="\n".join(f"- {c}" for c in task.criteria),
                lane=lane,
                project=task.scope,
            )
            self._tasks.set_ticket_ref(task.id, ticket.id)
            self._record_push(ticket.id, task.id, lane, flagged=False)
            self._log.append(
                EventKind.BOARD_WRITTEN,
                "kestrel",
                {"ticket_id": ticket.id, "action": "created", "lane": lane},
                task_id=task.id,
            )
            writes += 1
            if kind is EventKind.TASK_CREATED:
                return writes
            task = self._tasks.get(task_id)  # ticket_ref now set

        ticket_id = task.ticket_ref
        assert ticket_id is not None
        lane = self._lane_map.get(task.state, "To Do")
        flagged = task.state in self._flag_states
        previous = self._sync_row(ticket_id)

        if previous is None or previous[0] != lane:
            await self._board.set_lane(ticket_id, lane)
            self._log.append(
                EventKind.BOARD_WRITTEN,
                "kestrel",
                {"ticket_id": ticket_id, "action": "set_lane", "lane": lane},
                task_id=task.id,
            )
            writes += 1

        if previous is None or previous[1] != flagged:
            await self._board.set_flag(ticket_id, flagged)
            self._log.append(
                EventKind.BOARD_WRITTEN,
                "kestrel",
                {"ticket_id": ticket_id, "action": "set_flag", "flagged": flagged},
                task_id=task.id,
            )
            writes += 1

        self._record_push(ticket_id, task.id, lane, flagged)

        reason = payload.get("reason")
        if kind is EventKind.TASK_STATE_CHANGED and reason:
            await self._board.add_comment(ticket_id, f"{task.state}: {reason}")
            self._log.append(
                EventKind.BOARD_WRITTEN,
                "kestrel",
                {"ticket_id": ticket_id, "action": "comment", "text": reason},
                task_id=task.id,
            )
            writes += 1

        return writes

    async def record_pr_link(self, task_id: str, pr_url: str | None) -> None:
        """Not driven by an event kind yet - GitHub integration is a later
        step - but exposed now so that step has somewhere to call into."""
        task = self._tasks.get(task_id)
        if task.ticket_ref is None:
            return
        await self._board.set_pr_link(task.ticket_ref, pr_url)
        self._log.append(
            EventKind.BOARD_WRITTEN,
            "kestrel",
            {"ticket_id": task.ticket_ref, "action": "set_pr_link", "pr_url": pr_url},
            task_id=task_id,
        )

    # -- poll: board -> task instructions -----------------------------------

    async def poll(self, now: datetime | None = None) -> list[Ticket]:
        """Returns the tickets that produced an instruction (moved manually).
        Everything else - including Kestrel's own writes reflected back - is
        filtered out before it ever reaches `tasks.transition`."""
        now = now or datetime.now(UTC)
        since, _ = self._cursor()
        changed = await self._board.changed_since(since)
        if not changed:
            return []

        instructed: list[Ticket] = []
        latest = since
        for ticket in changed:
            if ticket.last_edited_time and (latest is None or ticket.last_edited_time > latest):
                latest = ticket.last_edited_time

            task = self._tasks.by_ticket_ref(ticket.id)
            if task is None:
                # A page in the database Kestrel doesn't know about - either
                # not a task yet, or created directly by Callum in Notion.
                # Turning that into a task is intake, which is a later step;
                # noted here rather than silently dropped.
                logger.info("board: ticket %s has no linked task, skipping", ticket.id)
                continue

            previous = self._sync_row(ticket.id)
            if (
                previous is not None
                and previous[0] == ticket.lane
                and previous[1] == ticket.flagged
            ):
                continue  # exactly what Kestrel last wrote - not a manual move

            self._record_push(ticket.id, task.id, ticket.lane, ticket.flagged)

            if previous is not None and previous[0] == ticket.lane:
                continue  # only the flag changed - not one of the two instructions below

            self._log.append(
                EventKind.BOARD_INSTRUCTION,
                "board",
                {
                    "ticket_id": ticket.id,
                    "from": previous[0] if previous else None,
                    "to": ticket.lane,
                },
                task_id=task.id,
            )
            self._apply_instruction(task.id, ticket.lane)
            instructed.append(ticket)

        if latest is not None:
            self._save_poll_cursor(latest)
        else:
            self._save_poll_cursor(now)
        return instructed

    def _apply_instruction(self, task_id: str, lane: str) -> None:
        """Sensible defaults for the two moves the design doc calls out by
        name. A move into a lane with no defined meaning is left alone rather
        than guessed at."""
        done_lane = self._lane_map.get(TaskState.DONE)
        queued_lane = self._lane_map.get(TaskState.CREATED)
        task = self._tasks.get(task_id)

        try:
            if lane == done_lane and task.state is not TaskState.DONE:
                self._tasks.transition(
                    task_id, TaskState.DONE, actor="board", reason="moved to Done"
                )
            elif lane == queued_lane and task.state not in (
                TaskState.CREATED,
                TaskState.BRIEFED,
                TaskState.PARKED,
                TaskState.DONE,
                TaskState.FAILED,
            ):
                # "Stop/requeue" - parking (not deleting) is the safe default:
                # the worktree persists and the task can be picked up again,
                # same as any other park.
                self._tasks.transition(
                    task_id, TaskState.PARKED, actor="board", reason="moved back to To Do"
                )
        except IllegalTransition as exc:
            logger.warning("board instruction ignored: %s", exc)
