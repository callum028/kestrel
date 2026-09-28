"""HTTP surface.

Three kinds of caller, and they want different things:

- **Claude Code hooks** push what a session is doing. This is what makes the
  stall detector real rather than synthetic: PreToolUse gives activity, Stop
  gives a claim of completion, Notification gives a question.
- **Clients** (desktop, phone) report attention signals and read state. They
  hold no history of their own - there is one conversation and it lives here.
- **Callum**, indirectly, acknowledging deliveries and driving tasks.

Every endpoint returns a state. Nothing returns an empty 200 that could mean
either success or a silent miss.
"""

from __future__ import annotations

import asyncio
import codecs
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from kestrel_agent.host_client import SessionHostUnavailable
from pydantic import BaseModel, Field

from .attention import Focus, Signals
from .auth import ALLOWED_ORIGINS, check_request, check_websocket, load_or_create_token
from .config import Config
from .events import EventKind
from .mail import render_untrusted
from .memory import Source
from .observations import ObservationState
from .outcomes import Refused
from .runtime import Runtime
from .tasks import IllegalTransition, TaskState
from .waits import WaitKind


class SignalsIn(BaseModel):
    app_focused: bool = False
    app_open: bool = False
    last_input_at: datetime | None = None
    heartbeat_at: datetime | None = None
    phone_on_tailnet: bool = False
    voice_session_open: bool = False
    calendar_busy: bool = False
    on_call: bool = False
    audio_route: str | None = None
    task_handle: str | None = None
    pane: str | None = None
    selection: str | None = None
    stated_away_until: datetime | None = None

    def to_signals(self, now: datetime) -> Signals:
        return Signals(
            now=now,
            app_focused=self.app_focused,
            app_open=self.app_open,
            last_input_at=self.last_input_at,
            heartbeat_at=self.heartbeat_at or now,
            phone_on_tailnet=self.phone_on_tailnet,
            voice_session_open=self.voice_session_open,
            calendar_busy=self.calendar_busy,
            on_call=self.on_call,
            audio_route=self.audio_route,
            focus=Focus(self.task_handle, self.pane, self.selection),
            stated_away_until=self.stated_away_until,
        )


class HookIn(BaseModel):
    """Claude Code hook payload. Field names verified against
    code.claude.com/docs/en/hooks.md (fetched directly, not summarised) as of
    this writing:

    - `Notification`: `message` (the notification text) and `notification_type`
      (e.g. `permission_prompt`, `idle_prompt`).
    - `Stop` / `SubagentStop`: `last_assistant_message` (the claim's own text -
      what claim validation scans for a stated blocker) and `stop_reason`.
    - `UserPromptSubmit`: `user_prompt` - also the signal that a session got
      past any startup dialog (see the startup-stall detector).
    - `SessionStart`: `session_start_reason`.

    Extras are tolerated regardless, because the shape is not ours to control
    and a docs update must never turn into a 422 on every hook firing.
    """

    hook_event_name: str
    session_id: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    message: str | None = None
    notification_type: str | None = None
    last_assistant_message: str | None = None
    stop_reason: str | None = None
    user_prompt: str | None = None
    session_start_reason: str | None = None

    model_config = {"extra": "allow"}


class WaitIn(BaseModel):
    """What `kestrel-wait` posts (see `kestrel_agent.kestrel_wait`) instead of
    the session arming its own watcher."""

    task_handle: str
    kind: WaitKind
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_minutes: float | None = None


class TaskIn(BaseModel):
    handle: str
    goal: str
    criteria: list[str] = Field(default_factory=list)
    executor: str = "claude_code"
    scope: str | None = None
    ticket_ref: str | None = None


class BindIn(BaseModel):
    session_id: str
    task_handle: str


class AckIn(BaseModel):
    on: str


class ConversationIn(BaseModel):
    text: str


class ReplyIn(BaseModel):
    text: str


class TicketIn(BaseModel):
    title: str
    body: str = ""
    lane: str | None = None
    project: str | None = None


class MemoryIn(BaseModel):
    fact: str
    category: str
    source: str = "explicit"
    scope: str = "global"
    task_ref: str | None = None
    core: bool = False


class ForgetIn(BaseModel):
    reason: str = ""


class RuleIn(BaseModel):
    rule: str
    scope: str = "global"


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscriptionIn(BaseModel):
    endpoint: str
    keys: SubscriptionKeys


class UnsubscribeIn(BaseModel):
    endpoint: str


class TerminalIn(BaseModel):
    cwd: str
    command: list[str] | None = None
    task_handle: str | None = None
    rows: int = 40
    cols: int = 120


class _TextStreamer:
    """Incremental UTF-8 decoding for one websocket's worth of terminal output.

    A PTY read can split a multibyte character across two chunks - a spinner
    or a box-drawing glyph lands mid-sequence as often as not, and Claude
    Code's UI is full of both. Decoding each chunk independently with
    `errors="replace"` (the naive version) renders that boundary as a mangled
    replacement character even though the bytes, taken together, are fine.
    `codecs`' incremental decoder holds the dangling partial sequence between
    calls instead of discarding it.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, chunk: bytes) -> str:
        return self._decoder.decode(chunk)

    def flush(self) -> str:
        """Call once, when the source has ended - a trailing incomplete
        sequence (the process died mid-write) becomes a replacement character
        instead of silently vanishing."""
        return self._decoder.decode(b"", final=True)


def _tool_signature(hook: HookIn) -> str:
    """Compact and stable, so identical calls collapse in the repetition counter
    while genuinely different work does not."""
    if not hook.tool_input:
        return ""
    for key in ("command", "file_path", "path", "pattern", "url"):
        if key in hook.tool_input:
            return str(hook.tool_input[key])[:120]
    return ""


def create_app(config: Config | None = None, runtime: Runtime | None = None) -> FastAPI:
    rt = runtime or Runtime.build(config or Config.from_env())
    token = load_or_create_token(rt.config.token_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Never assume a task believed running is still healthy just because
        # it was last seen that way - checked against the session host before
        # the first tick, and reported if it is not there.
        await rt.reconcile_on_startup()
        ticker = asyncio.create_task(rt.run())
        try:
            yield
        finally:
            ticker.cancel()
            # Only this process's connection - the session host and every
            # terminal it holds keep running past this point, which is the
            # entire point of hosting them elsewhere.
            await rt.terminals.disconnect()

    app = FastAPI(title="Kestrel", lifespan=lifespan)
    app.state.runtime = rt
    app.state.token = token

    @app.middleware("http")
    async def require_token(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Everything is behind this. There is no unauthenticated read-only
        # surface, because "what am I working on" is not public either.
        #
        # Preflights are exempt: a CORS preflight carries no Authorization
        # header by definition, so rejecting it would block the desktop app
        # before it ever got to authenticate. CORSMiddleware answers those,
        # and only for origins on the allowlist.
        if request.method == "OPTIONS":
            return await call_next(request)
        try:
            check_request(request, token)
        except HTTPException as exc:
            return JSONResponse(
                {"status": "refused", "reason": exc.detail}, status_code=exc.status_code
            )
        return await call_next(request)

    # Added last, so it sits outermost and handles preflights before auth runs.
    # The desktop app is served from tauri://localhost and talks to the API on
    # another origin, which makes every call cross-origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(ALLOWED_ORIGINS),
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["authorization", "content-type"],
        # No cookies anywhere in this system, deliberately: the token is a
        # header precisely so a browser cannot attach it automatically.
        allow_credentials=False,
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "active_tasks": len(rt.tasks.active()),
            "dev_lock": rt.dev_lock.holder().task_id if rt.dev_lock.holder() else None,
            "pending_deliveries": len(rt.deliveries.pending()),
        }

    @app.get("/state")
    def state() -> dict[str, Any]:
        now = datetime.now(UTC)
        attention = rt.attention(now)
        return {
            "block": rt.state_block(now),
            "presence": attention.presence,
            "focus": attention.focus.describe(),
            "speech_suppressed": attention.speech_suppressed,
        }

    @app.post("/clients/signals")
    def signals(body: SignalsIn) -> dict[str, Any]:
        state = rt.report_signals(body.to_signals(datetime.now(UTC)))
        return {"presence": state.presence, "speech_suppressed": state.speech_suppressed}

    @app.post("/hooks/claude")
    async def claude_hook(hook: HookIn) -> dict[str, Any]:
        task_id = rt.task_for_session(hook.session_id) if hook.session_id else None
        if task_id is None:
            # Recorded anyway. An unattributed hook is a gap in supervision, not
            # something to drop on the floor.
            rt.log.append(
                EventKind.HOOK_UNATTRIBUTED,
                "claude",
                {"event": hook.hook_event_name, "session_id": hook.session_id},
            )
            return {"status": "not_found", "searched_for": hook.session_id or "(no session_id)"}

        event = hook.hook_event_name
        if event in ("PreToolUse", "PostToolUse"):
            rt.log.append(
                EventKind.TOOL_CALL,
                "claude",
                {"tool": hook.tool_name or "?", "args": _tool_signature(hook)},
                task_id=task_id,
            )
        elif event == "Notification":
            rt.log.append(
                EventKind.TASK_QUESTION,
                "claude",
                {"message": hook.message, "notification_type": hook.notification_type},
                task_id=task_id,
            )
        elif event == "UserPromptSubmit":
            # Also the signal a session got past any startup dialog - see the
            # startup-stall detector in supervision.py.
            rt.log.append(
                EventKind.PROMPT_SUBMITTED, "claude", {"prompt": hook.user_prompt}, task_id=task_id
            )
        elif event in ("Stop", "SubagentStop"):
            # A claim, not a fact. Validation decides - and does so here,
            # synchronously, so a bad claim gets pushed back before the
            # session has moved on to anything else.
            rt.log.append(
                EventKind.TASK_CLOSED,
                "claude",
                {
                    "claimed": "done",
                    "last_assistant_message": hook.last_assistant_message,
                    # Same text as `last_assistant_message`, under the name
                    # `task_detail`/`kestrel-mcp`'s `task_detail` tool (and the
                    # phone UI) actually look for - Claude's own words, kept
                    # verbatim as the task's final report once it closes.
                    "report": hook.last_assistant_message,
                    "stop_reason": hook.stop_reason,
                },
                task_id=task_id,
            )
            await rt.check_completion_claim(task_id, hook.last_assistant_message)
        return {"status": "ok", "task_id": task_id, "event": event}

    @app.post("/waits")
    def register_wait(body: WaitIn) -> dict[str, Any]:
        outcome = rt.register_wait(body.task_handle, body.kind, body.params, body.timeout_minutes)
        return outcome.model_dump()

    @app.get("/waits")
    def list_waits() -> list[dict[str, Any]]:
        return [
            {
                "id": w.id,
                "task_id": w.task_id,
                "kind": w.kind,
                "params": w.params,
                "deadline": w.deadline.isoformat(),
            }
            for w in rt.waits.active()
        ]

    @app.post("/sessions/bind")
    def bind(body: BindIn) -> dict[str, Any]:
        task = rt.tasks.by_handle(body.task_handle)
        if task is None:
            return {"status": "not_found", "searched_for": body.task_handle, "looked_in": "tasks"}
        rt.bind_session(body.session_id, task.id)
        rt.log.append(
            EventKind.SESSION_BOUND, "agent", {"session_id": body.session_id}, task_id=task.id
        )
        return {"status": "ok", "task_id": task.id}

    @app.get("/tasks")
    def list_tasks() -> list[dict[str, Any]]:
        return [
            {
                "handle": t.handle,
                "goal": t.goal,
                "state": t.state,
                "executor": t.executor,
                "nudges": t.nudges,
                "criteria": t.criteria,
            }
            for t in rt.tasks.active()
        ]

    @app.post("/tasks")
    async def create_task(body: TaskIn) -> dict[str, Any]:
        if rt.tasks.by_handle(body.handle) is not None:
            return {"status": "refused", "reason": f"{body.handle} already exists"}
        task = rt.tasks.create(
            handle=body.handle,
            goal=body.goal,
            criteria=body.criteria,
            executor=body.executor,
            scope=body.scope,
            ticket_ref=body.ticket_ref,
        )
        # A task with a registered executor is briefed and started right
        # away - creating one (from the board, from the brain's start_task
        # tool, or directly) is "go do this now", not "queue this for later".
        # Left at CREATED, with no executor registered for it (a kind not
        # built yet, or the Claude Code executor simply not configured) -
        # the same gap /tasks/{handle}/retry already tolerates.
        executor = rt.executors.get(task.executor)
        if executor is not None:
            task = rt.tasks.transition(task.id, TaskState.BRIEFED, reason="executor available")
            task = rt.tasks.transition(task.id, TaskState.RUNNING, reason="started")
            await executor.start(task, brief=task.goal)
        return {"status": "ok", "id": task.id, "handle": task.handle, "state": task.state}

    @app.post("/tasks/{handle}/state/{to}")
    def transition(handle: str, to: TaskState) -> dict[str, Any]:
        task = rt.tasks.by_handle(handle)
        if task is None:
            return {"status": "not_found", "searched_for": handle, "looked_in": "tasks"}
        try:
            updated = rt.tasks.transition(task.id, to)
        except IllegalTransition as exc:
            return {"status": "refused", "reason": str(exc)}
        return {"status": "ok", "state": updated.state}

    @app.get("/events")
    def events(since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return [
            {
                "seq": e.seq,
                "ts": e.ts.isoformat(),
                "kind": e.kind,
                "actor": e.actor,
                "task_id": e.task_id,
                "payload": e.payload,
            }
            for e in rt.log.since(since, limit)
        ]

    @app.get("/deliveries")
    def deliveries() -> list[dict[str, Any]]:
        return [
            {
                "id": d.id,
                "subject": d.subject,
                "body": d.body,
                "channel": d.channel,
                "urgency": d.urgency,
                "escalations": d.escalations,
            }
            for d in rt.deliveries.pending()
        ]

    @app.post("/deliveries/{delivery_id}/ack")
    def ack(delivery_id: str, body: AckIn) -> dict[str, Any]:
        try:
            rt.deliveries.get(delivery_id)
        except KeyError:
            return {"status": "not_found", "searched_for": delivery_id, "looked_in": "deliveries"}
        # Acknowledging anywhere clears it everywhere - the Pi owns the state.
        rt.deliveries.acknowledge(delivery_id, on=body.on)
        return {"status": "ok"}

    @app.get("/observations")
    def observations() -> list[dict[str, Any]]:
        return [
            {"id": o.id, "what": o.what, "location": o.location, "why": o.why}
            for o in rt.observations.open()
        ]

    @app.post("/observations/{obs_id}/{state}")
    def resolve_observation(obs_id: str, state: ObservationState) -> dict[str, Any]:
        rt.observations.resolve(obs_id, state)
        return {"status": "ok", "state": state}

    # --- conversation ---------------------------------------------------------
    # The one Kestrel chat, shared across every device - see conversation.py
    # for the brain-step contract. Distinct from a Claude chat (raw terminal
    # output, one per session); this is Kestrel's own words.

    @app.post("/conversation/messages")
    async def post_conversation_message(body: ConversationIn) -> dict[str, Any]:
        message = await rt.conversation.post_user_message(body.text)
        return {"status": "ok", **message.to_dict()}

    @app.get("/conversation/messages")
    def list_conversation_messages(after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return [m.to_dict() for m in rt.conversation.since(after, limit)]

    @app.get("/conversation/status")
    def conversation_status() -> dict[str, Any]:
        # The visible "thinking" state the brain step asks for: a reply is
        # being generated in the background (see conversation.py), and there
        # is nothing to poll for yet except this flag - the reply itself
        # shows up via the messages endpoint like anything else Kestrel says.
        return {"thinking": rt.conversation.thinking}

    # --- web push ---------------------------------------------------------
    # Notifications route through Kestrel's own delivery/attention model
    # (docs/design.md §4.6) - this is the transport that reaches a closed app,
    # plugged into DeliveryTracker as the PHONE_NOTIFICATION channel's side
    # effect in Runtime.build. Subscribing/unsubscribing is a client telling
    # Kestrel where it lives, not a decision about what gets sent.

    @app.get("/push/vapid-public-key")
    def vapid_public_key() -> dict[str, Any]:
        return {"public_key": rt.vapid_public_key}

    @app.post("/push/subscriptions")
    def add_subscription(body: SubscriptionIn) -> dict[str, Any]:
        rt.push_subscriptions.add(body.endpoint, body.keys.p256dh, body.keys.auth)
        return {"status": "ok"}

    @app.delete("/push/subscriptions")
    def remove_subscription(body: UnsubscribeIn) -> dict[str, Any]:
        removed = rt.push_subscriptions.remove(body.endpoint)
        if not removed:
            return {
                "status": "not_found",
                "searched_for": body.endpoint,
                "looked_in": "subscriptions",
            }
        return {"status": "ok"}

    # --- terminals ----------------------------------------------------------
    # Terminals are a first-class surface, not a session viewer: N of them, some
    # bound to a task and its worktree, some just a shell in a directory. The
    # PTY lives in the session host, a separate long-lived process, so neither
    # a client leaving nor the server itself restarting kills the work. Every
    # call below is a round trip to that process, so all of them are async now.

    def _unavailable(exc: SessionHostUnavailable) -> dict[str, Any]:
        # A missing session host is not "no terminals" - it is a different,
        # more serious thing, and it must not look the same as an empty list.
        return {"status": "unavailable", "reason": str(exc)}

    @app.post("/terminals")
    async def open_terminal(body: TerminalIn) -> dict[str, Any]:
        task_id = None
        if body.task_handle:
            task = rt.tasks.by_handle(body.task_handle)
            if task is None:
                return {
                    "status": "not_found",
                    "searched_for": body.task_handle,
                    "looked_in": "tasks",
                }
            task_id = task.id
        cwd = Path(body.cwd).expanduser()
        if not cwd.is_dir():
            return {"status": "not_found", "searched_for": str(cwd), "looked_in": "filesystem"}
        try:
            terminal = await rt.terminals.create(
                cwd=cwd, command=body.command, task_id=task_id, rows=body.rows, cols=body.cols
            )
        except SessionHostUnavailable as exc:
            return _unavailable(exc)
        return {
            "status": "ok",
            "id": terminal.id,
            "cwd": str(terminal.cwd),
            "task_id": terminal.task_id,
        }

    @app.get("/terminals")
    async def list_terminals() -> list[dict[str, Any]] | dict[str, Any]:
        try:
            terminals = await rt.terminals.list()
        except SessionHostUnavailable as exc:
            return _unavailable(exc)
        return [
            {
                "id": t.id,
                "cwd": str(t.cwd),
                "task_id": t.task_id,
                "alive": t.alive,
                "command": t.command,
            }
            for t in terminals
        ]

    @app.delete("/terminals/{terminal_id}")
    async def close_terminal(terminal_id: str) -> dict[str, Any]:
        try:
            closed = await rt.terminals.close(terminal_id)
        except SessionHostUnavailable as exc:
            return _unavailable(exc)
        if not closed:
            return {"status": "not_found", "searched_for": terminal_id, "looked_in": "terminals"}
        return {"status": "ok"}

    @app.websocket("/terminals/{terminal_id}/ws")
    async def terminal_ws(websocket: WebSocket, terminal_id: str) -> None:
        # Middleware does not run for websockets, so this is checked here or
        # not at all - and this is the endpoint that carries keystrokes.
        if not check_websocket(websocket, token):
            await websocket.close(code=4401, reason="bad or missing token")
            return

        try:
            terminal = await rt.terminals.get(terminal_id)
        except SessionHostUnavailable as exc:
            await websocket.close(code=4503, reason=str(exc)[:120])
            return
        if terminal is None:
            await websocket.close(code=4404, reason="no such terminal")
            return

        await websocket.accept()
        queue = await terminal.subscribe()
        text = _TextStreamer()

        async def pump_out() -> None:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    trailing = text.flush()
                    if trailing:
                        await websocket.send_text(trailing)
                    await websocket.send_json({"type": "exit", "code": terminal.exit_code})
                    return
                decoded = text.feed(chunk)
                if decoded:
                    await websocket.send_text(decoded)

        async def pump_in() -> None:
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "input":
                    # The only path a human's keystrokes take - an executor
                    # writes into the same PTY straight through
                    # `rt.terminals`, never through this websocket - so this
                    # is the one true place to record "a human is here".
                    rt.human_activity.mark(terminal_id)
                    await terminal.write(message["data"].encode())
                elif message.get("type") == "resize":
                    await terminal.resize(int(message["rows"]), int(message["cols"]))

        try:
            await asyncio.gather(pump_out(), pump_in())
        except (WebSocketDisconnect, SessionHostUnavailable):
            pass
        finally:
            # The client goes; the session stays. That is the whole point.
            await terminal.unsubscribe(queue)

    @app.post("/tick")
    async def tick() -> dict[str, Any]:
        report = await rt.tick_once()
        return {
            "quiet": report.quiet,
            "nudged": report.nudged,
            "restarted": report.restarted,
            "parked": report.parked,
            "escalated": report.escalated,
            "stuck_deploy": report.stuck_deploy,
        }

    @app.get("/memory")
    def memory(project: str | None = None) -> dict[str, Any]:
        return {
            "core": rt.memory.core(project),
            "count": len(rt.memory.all()),
        }

    # --- brain step: task detail/control, board, mail, memory writes -------
    # New surface for kestrel-mcp (server/kestrel/brain/mcp_server.py) - the
    # brain never talks to Notion/Graph/the task store directly, it goes
    # through this same authenticated HTTP boundary like every other caller.

    def _outcome_dict(outcome: Any) -> dict[str, Any]:
        if isinstance(outcome, Refused):
            return {"status": "refused", "reason": outcome.reason}
        return {"status": "ok"}

    def _ticket_dict(ticket: Any) -> dict[str, Any]:
        return {
            "id": ticket.id,
            "handle": ticket.handle,
            "title": ticket.title,
            "lane": ticket.lane,
            "url": ticket.url,
            "project": ticket.project,
            "flagged": ticket.flagged,
            "pr_url": ticket.pr_url,
        }

    @app.get("/tasks/{handle}")
    async def task_detail(handle: str) -> dict[str, Any]:
        task = rt.tasks.by_handle(handle)
        if task is None:
            return {"status": "not_found", "searched_for": handle, "looked_in": "tasks"}

        events = rt.log.for_task(task.id)
        answered = {
            e.payload.get("question_seq")
            for e in events
            if e.kind == EventKind.TASK_ANSWERED and e.payload.get("question_seq") is not None
        }
        pending_question = None
        for e in reversed(events):
            if e.kind == EventKind.TASK_QUESTION and e.seq not in answered:
                pending_question = e.payload.get("message")
                break

        # `ci` is GitHub's own pre-merge check on the PR branch (CI_CHECKED,
        # from the orchestrator's landing gate) - `validation` is Kestrel's
        # separate, post-merge authoritative run against dev (VALIDATION_RUN).
        # They used to be conflated under one `ci` key pulling only the
        # latter, which meant a task still waiting on GitHub CI (or one that
        # never reaches validation at all, e.g. no validation configured)
        # reported nothing here at all.
        ci = next((e.payload for e in reversed(events) if e.kind == EventKind.CI_CHECKED), None)
        validation = next(
            (e.payload for e in reversed(events) if e.kind == EventKind.VALIDATION_RUN), None
        )
        final_report = next(
            (
                e.payload.get("report")
                for e in reversed(events)
                if e.kind == EventKind.TASK_CLOSED and e.payload.get("report")
            ),
            None,
        )

        pr_url = None
        ticket_url = None
        if task.ticket_ref:
            ticket = await rt.board.get(task.ticket_ref)
            if ticket is not None:
                pr_url = ticket.pr_url
                ticket_url = ticket.url

        queued = task.id in rt.dev_lock.queue()
        holder = rt.dev_lock.holder()
        pending_wait = queued or (holder is not None and holder.task_id == task.id)

        return {
            "status": "ok",
            "handle": task.handle,
            "goal": task.goal,
            "state": task.state,
            "executor": task.executor,
            "nudges": task.nudges,
            "criteria": task.criteria,
            "time_in_state_seconds": (datetime.now(UTC) - task.updated_at).total_seconds(),
            "pr_url": pr_url,
            "ticket_url": ticket_url,
            "ci": ci,
            "validation": validation,
            "pending_wait": pending_wait,
            "pending_question": pending_question,
            "final_report": final_report,
        }

    @app.post("/tasks/{handle}/reply")
    async def reply_to_task(handle: str, body: ReplyIn) -> dict[str, Any]:
        """The user's words, verbatim, into the task's session - see
        docs/design.md §1 ("Claude's questions reach the user verbatim... the
        user's answers reach Claude verbatim"). Surfaces whatever the
        executor's `send` returns, including a deferred `Refused` (a human is
        already typing in that session) rather than pretending it landed."""
        task = rt.tasks.by_handle(handle)
        if task is None:
            return {"status": "not_found", "searched_for": handle, "looked_in": "tasks"}
        executor = rt.executors.get(task.executor)
        if executor is None:
            return {"status": "refused", "reason": f"no {task.executor} executor registered"}
        outcome = await executor.send(task, body.text)
        return _outcome_dict(outcome)

    @app.post("/tasks/{handle}/stop")
    async def stop_task(handle: str) -> dict[str, Any]:
        task = rt.tasks.by_handle(handle)
        if task is None:
            return {"status": "not_found", "searched_for": handle, "looked_in": "tasks"}
        executor = rt.executors.get(task.executor)
        if executor is not None:
            await executor.stop(task, reason="stopped by user")
        try:
            updated = rt.tasks.transition(
                task.id, TaskState.PARKED, actor="callum", reason="stopped by user"
            )
        except IllegalTransition as exc:
            return {"status": "refused", "reason": str(exc)}
        return {"status": "ok", "state": updated.state}

    @app.post("/tasks/{handle}/retry")
    async def retry_task(handle: str) -> dict[str, Any]:
        task = rt.tasks.by_handle(handle)
        if task is None:
            return {"status": "not_found", "searched_for": handle, "looked_in": "tasks"}
        executor = rt.executors.get(task.executor)
        if executor is None:
            return {"status": "refused", "reason": f"no {task.executor} executor registered"}
        try:
            updated = rt.tasks.transition(
                task.id, TaskState.RUNNING, actor="callum", reason="retry"
            )
        except IllegalTransition as exc:
            return {"status": "refused", "reason": str(exc)}
        await executor.start(task, brief=task.goal)
        return {"status": "ok", "state": updated.state}

    @app.get("/board/tickets/find")
    async def find_ticket(handle: str) -> dict[str, Any]:
        ticket = await rt.board.find_by_handle(handle)
        if ticket is None:
            return {"status": "not_found", "searched_for": handle, "looked_in": "board"}
        return {"status": "ok", **_ticket_dict(ticket)}

    @app.post("/board/tickets")
    async def create_ticket(body: TicketIn) -> dict[str, Any]:
        ticket = await rt.board.create(
            title=body.title, body=body.body, lane=body.lane, project=body.project
        )
        return {"status": "ok", **_ticket_dict(ticket)}

    @app.get("/board/tickets")
    async def list_board() -> list[dict[str, Any]]:
        tickets = await rt.board.changed_since(None)
        return [_ticket_dict(t) for t in tickets]

    @app.get("/mail/recent")
    async def mail_recent(
        limit: int = 10, sender: str | None = None, query: str | None = None
    ) -> list[dict[str, Any]]:
        summaries = await rt.mail.recent(limit=limit, sender=sender, query=query)
        return [
            {
                "id": m.id,
                "sender": m.sender,
                "subject": m.subject,
                "received": m.received.isoformat(),
                "preview": m.preview,
            }
            for m in summaries
        ]

    @app.get("/mail/{message_id}")
    async def mail_get(message_id: str) -> dict[str, Any]:
        message = await rt.mail.get(message_id)
        if message is None:
            return {"status": "not_found", "searched_for": message_id, "looked_in": "mail"}
        # Untrusted-wrapped, per mail.py's rule: content is never handed to a
        # model without the boundary marker that says it is data, not
        # instructions.
        return {"status": "ok", "id": message.id, "untrusted": render_untrusted(message)}

    @app.post("/mail/{message_id}/to-task")
    async def email_to_task(message_id: str) -> dict[str, Any]:
        message = await rt.mail.get(message_id)
        if message is None:
            return {"status": "not_found", "searched_for": message_id, "looked_in": "mail"}
        wrapped = render_untrusted(message)
        ticket = await rt.board.create(title=f"From email: {message.subject}", body=wrapped)
        if rt.tasks.by_handle(ticket.handle) is not None:
            return {"status": "refused", "reason": f"{ticket.handle} already exists"}
        task = rt.tasks.create(
            handle=ticket.handle,
            goal=f"Triage email: {message.subject}",
            criteria=[],
            executor="kestrel",
            ticket_ref=ticket.id,
        )
        return {"status": "ok", "handle": task.handle, "ticket_url": ticket.url}

    @app.post("/memory")
    def remember(body: MemoryIn) -> dict[str, Any]:
        try:
            source = Source(body.source)
        except ValueError:
            return {"status": "refused", "reason": f"unknown memory source: {body.source!r}"}
        entry = rt.memory.write(
            fact=body.fact,
            category=body.category,
            source=source,
            scope=body.scope,
            task_ref=body.task_ref,
            core=body.core,
        )
        return {"status": "ok", "id": entry.id}

    @app.post("/memory/{entry_id}/forget")
    def forget(entry_id: str, body: ForgetIn) -> dict[str, Any]:
        try:
            rt.memory.forget(entry_id, reason=body.reason)
        except FileNotFoundError:
            return {"status": "not_found", "searched_for": entry_id, "looked_in": "memory"}
        return {"status": "ok"}

    @app.get("/memory/all")
    def list_memories(project: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "id": e.id,
                "fact": e.fact,
                "category": e.category,
                "source": e.source,
                "scope": e.scope,
                "core": e.core,
                "created_at": e.created_at.isoformat(),
            }
            for e in rt.memory.scoped(project)
        ]

    @app.post("/rules")
    def add_rule(body: RuleIn) -> dict[str, Any]:
        # Sugar over a memory write: a rule is just a fact that is always
        # injected (`core=True`) and categorised so it reads as a standing
        # instruction rather than a preference.
        entry = rt.memory.write(
            fact=body.rule, category="rule", source=Source.EXPLICIT, scope=body.scope, core=True
        )
        return {"status": "ok", "id": entry.id}

    return app
