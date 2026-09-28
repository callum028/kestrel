"""The server's side of the session host protocol.

`TerminalManager` used to be constructed directly inside the server process;
now the PTYs live in a separate long-lived process (`kestrel_agent.host`) and
this is a client of it. It exposes the same shape the server's API layer
already used - `create` / `list` / `get` / `close` / `close_all`, and a
`Terminal`-like object with `.write()`, `.resize()`, `.subscribe()`,
`.unsubscribe()` - so the API routes needed only to grow `await`, not to be
redesigned.

Connection is lazy: nothing is attempted until the first call. A session host
that is not running is not a bug to route around silently - "failures must be
legible" - so every call surfaces `SessionHostUnavailable` with a message that
says what to do about it, rather than hanging or returning an empty list that
looks like "no terminals" instead of "no host".
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SessionHostUnavailable(RuntimeError):
    """The session host is not reachable - not running, or its socket has
    gone away. Distinct from "terminal not found", which is a normal, expected
    outcome; this one means the whole subsystem is down."""


@dataclass
class RemoteTerminal:
    """A client-side handle onto a `Terminal` living in the session host
    process. Mutated in place by the client's read loop as exit events for
    subscriptions on this terminal arrive, mirroring how the real `Terminal`
    updates itself when its PTY closes."""

    id: str
    cwd: Path
    command: list[str]
    task_id: str | None
    alive: bool
    exit_code: int | None
    _client: SessionHostClient = field(repr=False)

    async def write(self, data: bytes) -> None:
        await self._client._call("write", terminal_id=self.id, data=_b64(data))

    async def resize(self, rows: int, cols: int) -> None:
        await self._client._call("resize", terminal_id=self.id, rows=rows, cols=cols)

    async def subscribe(self) -> asyncio.Queue[bytes | None]:
        return await self._client._subscribe(self)

    async def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        await self._client._unsubscribe(queue)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class SessionHostClient:
    def __init__(self, socket_path: Path):
        self._socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._subscriptions: dict[str, tuple[RemoteTerminal, asyncio.Queue[bytes | None]]] = {}
        self._queue_sub_ids: dict[int, str] = {}
        self._terminals: dict[str, RemoteTerminal] = {}

    # --- connection lifecycle ------------------------------------------------

    async def _ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._writer is not None:
                return
            try:
                reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                raise SessionHostUnavailable(
                    f"session host not reachable at {self._socket_path} "
                    "- start it with `python -m kestrel_agent`"
                ) from exc
            self._reader, self._writer = reader, writer
            self._reader_task = asyncio.create_task(self._read_loop())

    async def disconnect(self) -> None:
        """Closes this client's own connection, without touching anything on
        the host. This is the shape a server restart takes: the process that
        held the connection goes away, the terminals do not."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
        self._drop_connection(SessionHostUnavailable("client disconnected"))

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                self._dispatch(json.loads(line))
        except asyncio.CancelledError:
            pass
        finally:
            self._drop_connection(SessionHostUnavailable("session host connection lost"))

    def _drop_connection(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        for _, queue in self._subscriptions.values():
            queue.put_nowait(None)
        self._subscriptions.clear()
        self._queue_sub_ids.clear()
        self._reader = None
        self._writer = None

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "reply":
            future = self._pending.pop(message["id"], None)
            if future is None or future.done():
                return
            if message.get("ok"):
                future.set_result(message.get("result"))
            else:
                future.set_exception(RuntimeError(message.get("error") or "session host error"))
        elif kind == "output":
            entry = self._subscriptions.get(message["sub_id"])
            if entry is not None:
                entry[1].put_nowait(base64.b64decode(message["data"]))
        elif kind == "exit":
            entry = self._subscriptions.get(message["sub_id"])
            if entry is not None:
                terminal, queue = entry
                terminal.alive = False
                terminal.exit_code = message.get("code")
                queue.put_nowait(None)

    # --- request/reply ---------------------------------------------------------

    async def _call(self, op: str, **params: Any) -> Any:
        await self._ensure_connected()
        assert self._writer is not None
        req_id = uuid.uuid4().hex
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        await self._send({"op": op, "id": req_id, **params})
        return await future

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._writer is not None
        line = json.dumps(payload).encode() + b"\n"
        async with self._write_lock:
            self._writer.write(line)
            await self._writer.drain()

    # --- terminal descriptors ---------------------------------------------------

    def _terminal_from(self, descriptor: dict[str, Any]) -> RemoteTerminal:
        existing = self._terminals.get(descriptor["id"])
        if existing is not None:
            existing.alive = descriptor["alive"]
            existing.exit_code = descriptor["exit_code"]
            return existing
        terminal = RemoteTerminal(
            id=descriptor["id"],
            cwd=Path(descriptor["cwd"]),
            command=descriptor["command"],
            task_id=descriptor["task_id"],
            alive=descriptor["alive"],
            exit_code=descriptor["exit_code"],
            _client=self,
        )
        self._terminals[terminal.id] = terminal
        return terminal

    # --- public API, mirroring the old in-process TerminalManager --------------

    async def create(
        self,
        cwd: Path | str,
        command: list[str] | None = None,
        task_id: str | None = None,
        rows: int = 40,
        cols: int = 120,
    ) -> RemoteTerminal:
        result = await self._call(
            "create", cwd=str(cwd), command=command, task_id=task_id, rows=rows, cols=cols
        )
        return self._terminal_from(result)

    async def list(self, task_id: str | None = None) -> list[RemoteTerminal]:
        result = await self._call("list", task_id=task_id)
        terminals = [self._terminal_from(d) for d in result["terminals"]]
        live_ids = {t.id for t in terminals}
        for stale_id in set(self._terminals) - live_ids:
            self._terminals.pop(stale_id, None)
        return terminals

    async def get(self, terminal_id: str) -> RemoteTerminal | None:
        result = await self._call("get", terminal_id=terminal_id)
        if result is None:
            self._terminals.pop(terminal_id, None)
            return None
        return self._terminal_from(result)

    async def close(self, terminal_id: str) -> bool:
        result = await self._call("close", terminal_id=terminal_id)
        self._terminals.pop(terminal_id, None)
        return bool(result["closed"])

    async def close_all(self) -> None:
        if self._writer is None:
            return  # never connected - nothing on the host to have opened.
        await self._call("close_all")
        self._terminals.clear()

    # --- subscriptions -----------------------------------------------------------

    async def _subscribe(self, terminal: RemoteTerminal) -> asyncio.Queue[bytes | None]:
        await self._ensure_connected()
        assert self._writer is not None
        sub_id = uuid.uuid4().hex
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._subscriptions[sub_id] = (terminal, queue)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[sub_id] = future
        try:
            await self._send({"op": "subscribe", "id": sub_id, "terminal_id": terminal.id})
            await future  # the ack that the host has registered us - errors surface here.
        except Exception:
            self._subscriptions.pop(sub_id, None)
            raise
        self._queue_sub_ids[id(queue)] = sub_id
        return queue

    async def _unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        sub_id = self._queue_sub_ids.pop(id(queue), None)
        if sub_id is None:
            return
        self._subscriptions.pop(sub_id, None)
        if self._writer is not None:
            try:
                await self._send({"op": "unsubscribe", "id": uuid.uuid4().hex, "sub_id": sub_id})
            except (BrokenPipeError, ConnectionResetError):
                pass  # the connection is already gone; there is nothing left to tell.
