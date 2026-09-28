"""PTYs.

The terminal has to be good enough to replace the one in VS Code, or Kestrel
becomes the second window and the whole desk story falls over. So this hosts *N*
terminals rather than one per task: some bound to a task and its worktree, some
just a shell in a project directory. Same primitive either way - the only
difference is whether a task owns it and whether Kestrel is allowed to write.

Three properties that matter more than they look:

- **Sessions outlive clients.** The PTY lives here, not in the UI, so closing
  the app does not kill the work and a second surface can attach to the same
  session and see the same bytes.
- **Scrollback is replayed on attach.** Reconnecting mid-task and seeing an
  empty terminal is indistinguishable from having lost the session.
- **Writes are serialised.** Kestrel injects nudges and relayed answers into the
  same PTY Callum types into. Without a lock those interleave mid-keystroke.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import signal
import struct
import subprocess
import termios
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

SCROLLBACK_BYTES = 256 * 1024
READ_CHUNK = 65536


@dataclass
class Terminal:
    id: str
    cwd: Path
    command: list[str]
    task_id: str | None
    process: subprocess.Popen[bytes]
    master_fd: int
    rows: int = 40
    cols: int = 120

    _scrollback: deque[bytes] = field(default_factory=deque)
    _scrollback_size: int = 0
    _subscribers: set[asyncio.Queue[bytes | None]] = field(default_factory=set)
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _exited: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.process.poll()

    # --- output ------------------------------------------------------------

    def _record(self, data: bytes) -> None:
        self._scrollback.append(data)
        self._scrollback_size += len(data)
        while self._scrollback_size > SCROLLBACK_BYTES and self._scrollback:
            self._scrollback_size -= len(self._scrollback.popleft())

    def scrollback(self) -> bytes:
        return b"".join(self._scrollback)

    def _broadcast(self, data: bytes | None) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(data)

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        """Scrollback first, then live output. None marks the process exiting.

        A short-lived process can exit (self.alive goes False) before the
        reader has ever run - the PTY read is driven by the event loop, which
        only turns over on the next await, while the child process runs
        concurrently on the OS scheduler. So "has it exited" is not "is
        there anything left to read": bytes can still be sitting unread in
        the kernel's PTY buffer. Gate the exit sentinel on _exited, which is
        only set once the reader has seen a real EOF (an empty read), i.e.
        once everything has actually been drained into scrollback.
        """
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        if self._scrollback:
            queue.put_nowait(self.scrollback())
        if self._exited.is_set():
            queue.put_nowait(None)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        self._subscribers.discard(queue)

    # --- input -------------------------------------------------------------

    async def write(self, data: bytes) -> None:
        """Serialised, because Callum and Kestrel share this PTY."""
        async with self._write_lock:
            os.write(self.master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def wait(self) -> int:
        await self._exited.wait()
        return self.exit_code or 0

    def terminate(self) -> None:
        """Kills the process group, not the process. A shell that spawned a
        Claude session leaves an orphan otherwise."""
        if not self.alive:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)


class TerminalManager:
    def __init__(self) -> None:
        self._terminals: dict[str, Terminal] = {}

    def create(
        self,
        cwd: Path | str,
        command: list[str] | None = None,
        task_id: str | None = None,
        rows: int = 40,
        cols: int = 120,
        env: dict[str, str] | None = None,
    ) -> Terminal:
        cwd = Path(cwd)
        command = command or [os.environ.get("SHELL", "/bin/bash"), "-l"]
        master_fd, slave_fd = os.openpty()

        def _become_session_leader() -> None:
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=_become_session_leader,  # noqa: PLW1509 - needed for a controlling tty
            env={**os.environ, "TERM": "xterm-256color", **(env or {})},
            close_fds=True,
        )
        os.close(slave_fd)
        os.set_blocking(master_fd, False)

        terminal = Terminal(
            id=uuid.uuid4().hex[:12],
            cwd=cwd,
            command=command,
            task_id=task_id,
            process=process,
            master_fd=master_fd,
            rows=rows,
            cols=cols,
        )
        terminal.resize(rows, cols)
        self._terminals[terminal.id] = terminal
        self._attach_reader(terminal)
        return terminal

    def _attach_reader(self, terminal: Terminal) -> None:
        loop = asyncio.get_running_loop()

        def _on_readable() -> None:
            try:
                data = os.read(terminal.master_fd, READ_CHUNK)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                # The slave end closed - the process is gone.
                data = b""
            if data:
                terminal._record(data)
                terminal._broadcast(data)
                return
            loop.remove_reader(terminal.master_fd)
            terminal._broadcast(None)
            terminal._exited.set()

        loop.add_reader(terminal.master_fd, _on_readable)

    def get(self, terminal_id: str) -> Terminal | None:
        return self._terminals.get(terminal_id)

    def list(self, task_id: str | None = None) -> list[Terminal]:
        terminals = list(self._terminals.values())
        if task_id is None:
            return terminals
        return [t for t in terminals if t.task_id == task_id]

    def close(self, terminal_id: str) -> bool:
        terminal = self._terminals.pop(terminal_id, None)
        if terminal is None:
            return False
        terminal.terminate()
        # Teardown can run after the loop has gone (process shutdown, test
        # teardown), and a double-remove is expected rather than interesting -
        # the reader detaches itself when the process exits.
        with contextlib.suppress(RuntimeError, OSError, ValueError):
            asyncio.get_running_loop().remove_reader(terminal.master_fd)
        with contextlib.suppress(OSError):
            os.close(terminal.master_fd)
        return True

    def close_all(self) -> None:
        for terminal_id in list(self._terminals):
            self.close(terminal_id)
