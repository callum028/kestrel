"""The session host — a standalone process that owns every PTY.

Deploying or restarting the server must never kill a running Claude Code
session. The only way to make that true is to stop hosting PTYs *in* the
server: this module is the process that does instead. It is long-lived,
started once and left running on the box, and the server is a client of it —
never its parent, so a server crash or redeploy cannot take a session down
with it.

Transport is a Unix domain socket in the Kestrel data directory, created
`0600`. That is deliberately not TCP: everything here runs on one box now (the
Pi, or WSL for dev), so there is nothing to reach over the network, and a UDS
gets the permission bit and the "only this machine" guarantee for free.

Protocol is newline-delimited JSON rather than HTTP. There is no routing,
content negotiation, or multipart body here — one long-lived connection from
the server, carrying small control requests and a lot of terminal bytes both
ways. `asyncio.start_unix_server` plus `json.dumps`/`json.loads` is a few dozen
lines and does not require pulling FastAPI/uvicorn into a package that has had
zero dependencies until now; the server already depends on those for its own
reasons and is where that weight belongs.

Each request line carries an `id`; the matching reply echoes it, so several
calls can be in flight on one connection. A `subscribe` request's `id` doubles
as a subscription handle: output and exit events for it are tagged with
`sub_id` so the client can route them back to the right local queue, and a
later `unsubscribe` names it to tear down just that one stream. This is a
thin network wrapper around the existing `TerminalManager` — replay is just
`Terminal.subscribe()`'s existing scrollback-then-live queue, forwarded byte
for byte, so the host does not need to know anything about replay itself.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from .terminals import Terminal, TerminalManager

logger = logging.getLogger("kestrel_agent.host")

SOCKET_FILENAME = "session-host.sock"


def default_socket_path(data_dir: Path) -> Path:
    return data_dir / SOCKET_FILENAME


def _descriptor(terminal: Terminal) -> dict[str, Any]:
    return {
        "id": terminal.id,
        "cwd": str(terminal.cwd),
        "command": terminal.command,
        "task_id": terminal.task_id,
        "alive": terminal.alive,
        "exit_code": terminal.exit_code,
    }


class _Connection:
    """One server connection: request dispatch, plus the forwarder tasks for
    whatever it has subscribed to. Writes are serialised because replies and
    pushed output/exit events all share one socket and one writer."""

    def __init__(
        self, manager: TerminalManager, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        self._manager = manager
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._forwarders: dict[str, asyncio.Task[None]] = {}

    async def _send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message).encode() + b"\n"
        async with self._write_lock:
            try:
                self._writer.write(line)
                await self._writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client is gone; the read loop will notice and clean up.

    async def _reply_ok(self, req_id: str, result: Any = None) -> None:
        await self._send({"type": "reply", "id": req_id, "ok": True, "result": result})

    async def _reply_error(self, req_id: str, error: str) -> None:
        await self._send({"type": "reply", "id": req_id, "ok": False, "error": error})

    async def run(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    return
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("session host: dropped a malformed line")
                    continue
                await self._handle(message)
        finally:
            self._teardown()

    def _teardown(self) -> None:
        """The connection is gone. Its subscriptions must go with it — the
        terminals themselves are untouched; only Callum closing them, or the
        process exiting on its own, ends a session."""
        for task in self._forwarders.values():
            task.cancel()
        self._forwarders.clear()

    async def _handle(self, message: dict[str, Any]) -> None:
        op = message.get("op")
        req_id = message.get("id")
        try:
            if op == "create":
                terminal = self._manager.create(
                    cwd=message["cwd"],
                    command=message.get("command"),
                    task_id=message.get("task_id"),
                    rows=message.get("rows", 40),
                    cols=message.get("cols", 120),
                )
                await self._reply_ok(req_id, _descriptor(terminal))
            elif op == "list":
                task_id = message.get("task_id")
                terminals = self._manager.list(task_id=task_id)
                await self._reply_ok(req_id, {"terminals": [_descriptor(t) for t in terminals]})
            elif op == "get":
                terminal = self._manager.get(message["terminal_id"])
                await self._reply_ok(req_id, _descriptor(terminal) if terminal else None)
            elif op == "write":
                terminal = self._manager.get(message["terminal_id"])
                if terminal is None:
                    await self._reply_error(req_id, "no such terminal")
                else:
                    await terminal.write(base64.b64decode(message["data"]))
                    await self._reply_ok(req_id)
            elif op == "resize":
                terminal = self._manager.get(message["terminal_id"])
                if terminal is None:
                    await self._reply_error(req_id, "no such terminal")
                else:
                    terminal.resize(int(message["rows"]), int(message["cols"]))
                    await self._reply_ok(req_id)
            elif op == "close":
                closed = self._manager.close(message["terminal_id"])
                await self._reply_ok(req_id, {"closed": closed})
            elif op == "close_all":
                self._manager.close_all()
                await self._reply_ok(req_id)
            elif op == "subscribe":
                await self._subscribe(req_id, message["terminal_id"])
            elif op == "unsubscribe":
                self._unsubscribe(message["sub_id"])
                # No reply: this is teardown, and there is nothing useful to
                # say back beyond it having happened.
            else:
                await self._reply_error(req_id, f"unknown op: {op}")
        except KeyError as exc:
            await self._reply_error(req_id, f"missing field: {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the connection
            await self._reply_error(req_id, f"{type(exc).__name__}: {exc}")

    async def _subscribe(self, sub_id: str, terminal_id: str) -> None:
        terminal = self._manager.get(terminal_id)
        if terminal is None:
            await self._reply_error(sub_id, "no such terminal")
            return
        queue = terminal.subscribe()
        await self._reply_ok(sub_id)
        self._forwarders[sub_id] = asyncio.create_task(self._forward(sub_id, terminal, queue))

    def _unsubscribe(self, sub_id: str) -> None:
        task = self._forwarders.pop(sub_id, None)
        if task is not None:
            task.cancel()

    async def _forward(
        self, sub_id: str, terminal: Terminal, queue: asyncio.Queue[bytes | None]
    ) -> None:
        """Scrollback then live output, exactly as `Terminal.subscribe()`
        already orders it — replay is not a special case here, just the first
        item(s) on the same queue."""
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    await self._send(
                        {
                            "type": "exit",
                            "sub_id": sub_id,
                            "terminal_id": terminal.id,
                            "code": terminal.exit_code,
                        }
                    )
                    return
                await self._send(
                    {
                        "type": "output",
                        "sub_id": sub_id,
                        "terminal_id": terminal.id,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    }
                )
        except asyncio.CancelledError:
            pass
        finally:
            terminal.unsubscribe(queue)


class SessionHost:
    """Owns the one `TerminalManager` for the box and the UDS server in front
    of it. Everything durable about a session lives in the `TerminalManager`;
    this class is only the network skin."""

    def __init__(self, socket_path: Path, manager: TerminalManager | None = None):
        self.socket_path = socket_path
        self.manager = manager or TerminalManager()
        self._server: asyncio.base_events.Server | None = None

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _Connection(self.manager, reader, writer).run()
        with contextlib.suppress(OSError):
            writer.close()

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # A stale socket from a previous run (killed rather than shut down)
        # must not block the bind - it is not a sign anything is still
        # listening, since a live listener would still own the inode fine
        # either way. Removing it before binding is the standard UDS dance.
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._on_connect, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.manager.close_all()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
