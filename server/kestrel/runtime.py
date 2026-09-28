"""Wiring, and the tick loop.

Everything durable is constructed once here and shared. The Pi owns the
conversation and all state; clients are views onto this object.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kestrel_agent.host_client import SessionHostClient

from .attention import AttentionState, Signals, compute
from .board import Board, build_board
from .board_sync import BoardSync
from .config import Config
from .db import Database, connect
from .delivery import DeliveryTracker
from .devlock import DevLock
from .events import EventKind, EventLog
from .executors import Executor
from .memory import MemoryStore
from .observations import ObservationStore
from .orchestrator import Orchestrator, TickReport
from .tasks import TaskStore

TICK_INTERVAL_SECONDS = 30


@dataclass
class Runtime:
    config: Config
    conn: Database
    log: EventLog
    tasks: TaskStore
    memory: MemoryStore
    observations: ObservationStore
    deliveries: DeliveryTracker
    dev_lock: DevLock
    orchestrator: Orchestrator
    terminals: SessionHostClient
    board: Board
    board_sync: BoardSync
    executors: dict[str, Executor] = field(default_factory=dict)

    # Latest raw report from whichever client last spoke. Sensors, not beliefs -
    # nothing here is ever inferred, and it is recomputed rather than remembered.
    _signals: Signals | None = None
    _last_board_poll: datetime | None = None

    @classmethod
    def build(
        cls,
        config: Config,
        executors: dict[str, Executor] | None = None,
        board: Board | None = None,
    ) -> Runtime:
        config.ensure_dirs()
        conn = connect(config.db_path)
        log = EventLog(conn)
        tasks = TaskStore(conn, log)
        deliveries = DeliveryTracker(conn, log)
        dev_lock = DevLock(conn)
        executors = executors or {}
        board = board or build_board(config.board)
        return cls(
            config=config,
            conn=conn,
            log=log,
            tasks=tasks,
            memory=MemoryStore(config.memory_repo, log),
            observations=ObservationStore(conn, log),
            deliveries=deliveries,
            dev_lock=dev_lock,
            orchestrator=Orchestrator(tasks, log, deliveries, dev_lock, executors),
            terminals=SessionHostClient(config.session_host_socket),
            board=board,
            board_sync=BoardSync(board, tasks, log, conn),
            executors=executors,
        )

    # --- attention ----------------------------------------------------------

    def report_signals(self, signals: Signals) -> AttentionState:
        previous = compute(self._signals) if self._signals else None
        self._signals = signals
        state = compute(signals)
        if previous is None or previous.presence is not state.presence:
            self.log.append(EventKind.PRESENCE_CHANGED, "client", {"presence": str(state.presence)})
        if previous is None or previous.focus != state.focus:
            self.log.append(EventKind.FOCUS_CHANGED, "client", {"focus": state.focus.describe()})
        return state

    def attention(self, now: datetime | None = None) -> AttentionState:
        """Stale signals decay into AWAY on their own, because a client that has
        stopped reporting is a client that is not there."""
        now = now or datetime.now(UTC)
        signals = self._signals or Signals(now=now)
        return compute(Signals(**{**signals.__dict__, "now": now}))

    def state_block(self, now: datetime | None = None) -> str:
        """The volatile block, arithmetic already done. Never enters history."""
        now = now or datetime.now(UTC)
        signals = self._signals or Signals(now=now)
        lines = [self.attention(now).describe(now, signals)]
        active = self.tasks.active()
        if active:
            lines.append("")
            lines.extend(f"{t.handle}: {t.state}, {t.nudges} nudges" for t in active)
        return "\n".join(lines)

    # --- sessions -----------------------------------------------------------

    def bind_session(self, session_id: str, task_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, task_id, started_at) VALUES (?, ?, ?)",
            (session_id, task_id, datetime.now(UTC).isoformat()),
        )

    def task_for_session(self, session_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT task_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row["task_id"] if row else None

    # --- the loop -----------------------------------------------------------

    async def tick_once(self, now: datetime | None = None) -> TickReport:
        now = now or datetime.now(UTC)
        report = await self.orchestrator.tick(now, self.attention(now))
        await self.sync_board(now)
        return report

    async def sync_board(self, now: datetime | None = None) -> None:
        """Push runs every tick - it is cheap, and only ever writes what
        changed since the last one. Poll is rate-limited separately (its own
        interval, configurable) because it costs a Notion API call regardless
        of whether anything moved."""
        now = now or datetime.now(UTC)
        await self.board_sync.push()
        interval = self.config.board.poll_interval_seconds
        due = self._last_board_poll is None or (now - self._last_board_poll).total_seconds() >= interval
        if due:
            await self.board_sync.poll(now)
            self._last_board_poll = now

    async def run(self, interval: float = TICK_INTERVAL_SECONDS) -> None:
        while True:
            try:
                await self.tick_once()
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                # Never swallowed. A failing tick must not stop the loop, but it
                # must leave a trace: an assistant that dies quietly is worse
                # than one that reports a bad pass.
                self.log.append(
                    EventKind.TICK_FAILED,
                    "kestrel",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
            await asyncio.sleep(interval)
