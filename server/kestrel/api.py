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
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .attention import Focus, Signals
from .auth import ALLOWED_ORIGINS, check_request, check_websocket, load_or_create_token
from .config import Config
from .events import EventKind
from .observations import ObservationState
from .runtime import Runtime
from .tasks import IllegalTransition, TaskState


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
    """Claude Code hook payload. Field names follow the hook contract; extras are
    tolerated because the shape is not ours to control."""

    hook_event_name: str
    session_id: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    message: str | None = None

    model_config = {"extra": "allow"}


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


class TerminalIn(BaseModel):
    cwd: str
    command: list[str] | None = None
    task_handle: str | None = None
    rows: int = 40
    cols: int = 120


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
        ticker = asyncio.create_task(rt.run())
        try:
            yield
        finally:
            ticker.cancel()

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
    def claude_hook(hook: HookIn) -> dict[str, Any]:
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
                EventKind.TASK_QUESTION, "claude", {"message": hook.message}, task_id=task_id
            )
        elif event in ("Stop", "SubagentStop"):
            # A claim, not a fact. Validation decides.
            rt.log.append(EventKind.TASK_CLOSED, "claude", {"claimed": "done"}, task_id=task_id)
        return {"status": "ok", "task_id": task_id, "event": event}

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
    def create_task(body: TaskIn) -> dict[str, Any]:
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

    # --- terminals ----------------------------------------------------------
    # Terminals are a first-class surface, not a session viewer: N of them, some
    # bound to a task and its worktree, some just a shell in a directory. The
    # PTY lives in the agent, so closing a client never kills the work.

    # These two must be async: they attach and detach an event-loop reader, and
    # FastAPI runs sync endpoints in a threadpool where there is no running loop.
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
        terminal = rt.terminals.create(
            cwd=cwd, command=body.command, task_id=task_id, rows=body.rows, cols=body.cols
        )
        return {
            "status": "ok",
            "id": terminal.id,
            "cwd": str(terminal.cwd),
            "task_id": terminal.task_id,
        }

    @app.get("/terminals")
    def list_terminals() -> list[dict[str, Any]]:
        return [
            {
                "id": t.id,
                "cwd": str(t.cwd),
                "task_id": t.task_id,
                "alive": t.alive,
                "command": t.command,
            }
            for t in rt.terminals.list()
        ]

    @app.delete("/terminals/{terminal_id}")
    async def close_terminal(terminal_id: str) -> dict[str, Any]:
        if not rt.terminals.close(terminal_id):
            return {"status": "not_found", "searched_for": terminal_id, "looked_in": "terminals"}
        return {"status": "ok"}

    @app.websocket("/terminals/{terminal_id}/ws")
    async def terminal_ws(websocket: WebSocket, terminal_id: str) -> None:
        # Middleware does not run for websockets, so this is checked here or
        # not at all - and this is the endpoint that carries keystrokes.
        if not check_websocket(websocket, token):
            await websocket.close(code=4401, reason="bad or missing token")
            return

        terminal = rt.terminals.get(terminal_id)
        if terminal is None:
            await websocket.close(code=4404, reason="no such terminal")
            return

        await websocket.accept()
        queue = terminal.subscribe()

        async def pump_out() -> None:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    await websocket.send_json({"type": "exit", "code": terminal.exit_code})
                    return
                await websocket.send_text(chunk.decode("utf-8", errors="replace"))

        async def pump_in() -> None:
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "input":
                    await terminal.write(message["data"].encode())
                elif message.get("type") == "resize":
                    terminal.resize(int(message["rows"]), int(message["cols"]))

        try:
            await asyncio.gather(pump_out(), pump_in())
        except WebSocketDisconnect:
            pass
        finally:
            # The client goes; the session stays. That is the whole point.
            terminal.unsubscribe(queue)

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

    return app
