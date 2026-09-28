"""Wiring, and the tick loop.

Everything durable is constructed once here and shared. The Pi owns the
conversation and all state; clients are views onto this object.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kestrel_agent.host_client import SessionHostClient

from .attention import AttentionState, Signals, Urgency, compute
from .board import Board, build_board
from .board_sync import BoardSync
from .brain.responder import BrainResponder
from .brain.runner import BrainConfig, BrainRunner, MCPServerSpec
from .config import Config
from .conversation import ConversationStore, Responder
from .db import Database, connect
from .delivery import DeliveryTracker
from .devlock import DevLock
from .events import EventKind, EventLog
from .executors import Executor, ExecutorKind
from .executors.claude_code import ClaudeCodeConfig, ClaudeCodeExecutor
from .github import GitHub, build_github
from .graph_mail import GraphMailConfig, GraphMailReader
from .mail import FakeMailReader, MailReader, RecordingMailReader
from .memory import MemoryStore
from .observations import ObservationStore
from .orchestrator import Orchestrator, TickReport
from .outcomes import NotFound, Ok, Outcome, Refused
from .push import PushSender, PushSubscriptionStore, load_or_create_vapid_keys
from .supervision import (
    Diff,
    extract_blocked_claim,
    introduced_blocker_markers,
    unrequested_spec_changes,
    validate_blocker,
)
from .tasks import Task, TaskState, TaskStore
from .terminal_activity import HumanActivityTracker
from .waits import WaitKind, WaitStore, default_deadline

logger = logging.getLogger("kestrel")

TICK_INTERVAL_SECONDS = 30


def _build_mail_reader(config: Config, log: EventLog) -> MailReader:
    """Real Graph access needs a completed one-time `mail_auth` sign-in as
    well as the app registration's IDs - either missing means there is
    nothing to authenticate with yet, so fall back to the fake rather than
    failing Runtime.build over a capability that is opt-in by design."""
    if config.mail_tenant_id and config.mail_client_id and config.mail_token_path.exists():
        inner: MailReader = GraphMailReader(
            GraphMailConfig(
                tenant_id=config.mail_tenant_id,
                client_id=config.mail_client_id,
                token_path=config.mail_token_path,
            )
        )
    else:
        logger.warning(
            "mail: no Graph credentials/token found - using the fake mail reader "
            "(run `python -m kestrel.mail_auth` after setting KESTREL_MAIL_TENANT_ID "
            "and KESTREL_MAIL_CLIENT_ID to read a real mailbox)"
        )
        inner = FakeMailReader()
    return RecordingMailReader(inner, log)


def _build_brain_responder(config: Config, rt: Runtime) -> BrainResponder:
    """The MCP server the brain's `claude -p` calls are restricted to
    (`--tools mcp__kestrel__*`, see runner.py): by default the same
    interpreter running this process, invoking `kestrel.brain.mcp_server` as
    a module - which is exactly what the `kestrel-mcp` console script does,
    just without needing it on PATH. `KESTREL_BRAIN_MCP_COMMAND` overrides
    this for a packaged/installed deployment.
    """
    mcp_command = config.brain_mcp_server_command or sys.executable
    mcp_args = (
        config.brain_mcp_server_args
        if config.brain_mcp_server_command
        else ("-m", "kestrel.brain.mcp_server")
    )
    mcp_env = {"KESTREL_SERVER_URL": config.server_url, "KESTREL_DATA": str(config.data_dir)}
    runner = BrainRunner(
        BrainConfig(
            binary=config.brain_claude_binary or "claude",
            base_args=config.brain_claude_base_args,
            timeout_seconds=config.brain_timeout_seconds,
            work_dir=config.brain_work_dir,
        )
    )
    return BrainResponder(
        runner=runner,
        identity_dir=config.identity_dir,
        memory=rt.memory,
        state_block=rt.state_block,
        active_tasks=rt.tasks.active,
        mcp_servers=[
            MCPServerSpec(name="kestrel", command=mcp_command, args=mcp_args, env=mcp_env)
        ],
    )


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
    mail: MailReader
    board: Board
    board_sync: BoardSync
    conversation: ConversationStore
    push_subscriptions: PushSubscriptionStore
    push: PushSender
    vapid_public_key: str
    waits: WaitStore
    github: GitHub | None
    human_activity: HumanActivityTracker = field(default_factory=HumanActivityTracker)
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
        responder: Responder | None = None,
    ) -> Runtime:
        config.ensure_dirs()
        conn = connect(config.db_path)
        log = EventLog(conn)
        tasks = TaskStore(conn, log)
        vapid_keys = load_or_create_vapid_keys(config.vapid_key_path)
        push_subscriptions = PushSubscriptionStore(conn, log)
        push = PushSender(vapid_keys, push_subscriptions, log)
        deliveries = DeliveryTracker(conn, log, on_phone_notify=push.notify_delivery)
        dev_lock = DevLock(conn)
        terminals = SessionHostClient(config.session_host_socket)
        human_activity = HumanActivityTracker()

        # Auto-registered only when the caller hasn't opted to hand its own
        # executors dict in (tests, mostly) and there is at least one project
        # configured to run Claude Code against - a bare `Config` built by
        # hand, as most tests do, has neither and gets the pre-existing empty
        # dict, unchanged.
        if executors is None and config.claude_projects:
            executors = {
                str(ExecutorKind.CLAUDE_CODE): ClaudeCodeExecutor(
                    config=ClaudeCodeConfig(
                        projects=config.claude_projects,
                        worktrees_root=config.worktrees_root,
                        server_url=config.server_url,
                        data_dir=config.data_dir,
                        claude_binary=config.claude_binary,
                        claude_base_args=config.claude_base_args,
                    ),
                    log=log,
                    terminals=terminals,
                    human_activity=human_activity,
                )
            }
        executors = executors or {}
        board = board or build_board(config.board)
        board_sync = BoardSync(board, tasks, log, conn)
        waits = WaitStore(conn, log)
        github = build_github(config)
        memory = MemoryStore(config.memory_repo, log)
        rt = cls(
            config=config,
            conn=conn,
            log=log,
            tasks=tasks,
            memory=memory,
            observations=ObservationStore(conn, log),
            deliveries=deliveries,
            dev_lock=dev_lock,
            orchestrator=Orchestrator(
                tasks,
                log,
                deliveries,
                dev_lock,
                executors,
                waits=waits,
                github=github,
                board_sync=board_sync,
                validation=config.validation,
            ),
            terminals=terminals,
            mail=_build_mail_reader(config, log),
            board=board,
            board_sync=board_sync,
            conversation=ConversationStore(conn, log, responder=responder),
            push_subscriptions=push_subscriptions,
            push=push,
            vapid_public_key=vapid_keys.public_key_b64,
            waits=waits,
            github=github,
            human_activity=human_activity,
            executors=executors,
        )

        # The brain: only wired in when a caller hasn't already supplied a
        # responder (tests mostly, and StubResponder's default is exactly
        # what most of them want) and a binary is configured
        # (KESTREL_BRAIN_CLAUDE_BINARY) - same backward-compatible gating as
        # the Claude Code executor above. Built after `rt` exists because the
        # brain needs `rt.state_block`/`rt.tasks.active`, which are Runtime's
        # own methods.
        if responder is None and config.brain_claude_binary:
            rt.conversation.set_responder(_build_brain_responder(config, rt))

        return rt

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

    # --- waits ----------------------------------------------------------------
    # See waits.py and kestrel_agent.kestrel_wait: a task registers what it is
    # waiting on instead of arming its own watcher, and the orchestrator polls
    # the real thing on the tick.

    def register_wait(
        self,
        task_handle: str,
        kind: WaitKind,
        params: dict[str, object],
        timeout_minutes: float | None = None,
        now: datetime | None = None,
    ) -> Outcome:
        task = self.tasks.by_handle(task_handle)
        if task is None:
            return NotFound(searched_for=task_handle, looked_in="tasks")
        now = now or datetime.now(UTC)
        deadline = default_deadline(kind, now, timeout_minutes)
        wait = self.waits.register(task.id, kind, params, deadline, actor="agent")
        return Ok(value={"wait_id": wait.id, "deadline": wait.deadline.isoformat()})

    # --- claim validation -------------------------------------------------------
    # "Blocked" is a claim exactly like "done" is a claim - both get checked
    # against the diff rather than taken on trust (supervision.py).

    async def check_completion_claim(
        self, task_id: str, last_assistant_message: str | None
    ) -> Outcome:
        task = self.tasks.get(task_id)
        executor = self.executors.get(task.executor)
        differ = getattr(executor, "diff", None)
        if differ is None:
            # No worktree to check against (not a Claude Code task, or the
            # executor is not registered) - nothing to validate.
            return Ok()

        diff: Diff = await differ(task)

        if not diff.files and not diff.added and not diff.removed:
            return await self._reject_claim(
                task, executor, f"{task.handle} claims done but the worktree diff is empty."
            )

        markers = introduced_blocker_markers(diff)
        if markers:
            return await self._reject_claim(
                task,
                executor,
                f"{task.handle} claims done but introduced: {'; '.join(markers)}",
            )

        spec_check = unrequested_spec_changes(diff, task.criteria)
        if spec_check.status == "ambiguous":
            candidates = ", ".join(spec_check.candidates)
            return await self._reject_claim(
                task,
                executor,
                f"{task.handle} claims done but edited tests the criteria didn't ask for: "
                f"{candidates}",
            )

        blocked = extract_blocked_claim(last_assistant_message)
        if blocked:
            exists = await executor.symbol_exists(task, blocked)
            result = validate_blocker(blocked, diff, {blocked} if exists else set())
            if isinstance(result, Refused):
                return await self._reject_claim(task, executor, f"{task.handle}: {result.reason}")

        self.log.append(EventKind.CLAIM_ACCEPTED, "kestrel", {}, task_id=task_id)
        return Ok()

    async def _reject_claim(self, task: Task, executor: Executor, evidence: str) -> Outcome:
        self.log.append(EventKind.CLAIM_REJECTED, "kestrel", {"reason": evidence}, task_id=task.id)
        await executor.send(
            task, f"{evidence} If the work is really done, say where it is; otherwise continue."
        )
        return Refused(reason=evidence)

    # --- restart reconciliation -------------------------------------------------

    async def reconcile_on_startup(self, now: datetime | None = None) -> list[str]:
        """Every task believed `RUNNING` is checked against the session host
        on startup - never assumed healthy just because it was last seen that
        way. Returns the handles found unaccountable; the ongoing per-tick
        `alive` check (orchestrator.py) takes care of actually parking them,
        so this is only responsible for making the fact visible immediately
        rather than waiting for the report to notice on its own."""
        now = now or datetime.now(UTC)
        unaccountable: list[str] = []
        for task in self.tasks.active():
            if task.state is not TaskState.RUNNING:
                continue
            executor = self.executors.get(task.executor)
            checker = getattr(executor, "alive", None)
            if checker is None:
                continue
            alive = await checker(task)
            if alive is not True:
                unaccountable.append(task.handle)
                self.log.append(
                    EventKind.RECONCILE_UNACCOUNTABLE,
                    "kestrel",
                    {"alive": alive},
                    task_id=task.id,
                )
        if unaccountable:
            self.deliveries.send(
                subject="restart: tasks unaccounted for",
                body=f"On restart, {len(unaccountable)} task(s) believed running had no live "
                f"session on the host: {', '.join(unaccountable)}. The next tick will act on it.",
                urgency=Urgency.NORMAL,
                state=self.attention(now),
                now=now,
            )
        return unaccountable

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
        due = (
            self._last_board_poll is None
            or (now - self._last_board_poll).total_seconds() >= interval
        )
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
