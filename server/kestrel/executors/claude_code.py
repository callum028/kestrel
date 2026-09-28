"""The Claude Code executor - code supervision, the first of the four kinds.

Every task run through this executor is an interactive `claude` session
inside a session-host PTY, in its own git worktree, on its own branch. "Every"
matters: there is deliberately no headless/attended split here (the design's
Agent-SDK-for-unattended-work idea is future work) - one shape, always
attachable from the web terminal, because §7 of the design promises that the
terminal is not a session viewer bolted on after the fact.

What this class owns:

- Standing up the worktree and the `.claude/settings.local.json` + brief file
  inside it (see `kestrel_agent.worktrees` / `kestrel_agent.claude_settings`
  for why each is shaped the way it is).
- Starting `claude` in that worktree inside a session-host PTY, tagged with
  `KESTREL_TASK_ID` so the hooks it fires can attribute themselves.
- Writing into that PTY for `send`, deferring instead when a human is
  currently typing into the same session.
- Ending the session for `stop`, without touching the worktree - the whole
  point of a worktree-per-task is that it survives the session that used it.

What it does not own: the stall/claim-validation logic in `supervision.py`
(this only carries out what the orchestrator decides), and the hook *contents*
themselves - those are handled by `kestrel_hook.py`, running as a separate
process launched by Claude Code, not by this class.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from kestrel_agent.claude_settings import write_brief, write_hooks_settings
from kestrel_agent.host_client import SessionHostClient
from kestrel_agent.worktrees import ensure_worktree

from ..events import EventKind, EventLog
from ..outcomes import Ok, Outcome, Refused
from ..supervision import Diff
from ..tasks import Task
from ..terminal_activity import HumanActivityTracker
from . import ExecutorKind

# What Claude is told to do instead of being handed the brief as a CLI
# argument. Passing the brief itself on argv would put arbitrary-length,
# arbitrary-content task text (backticks, quotes, newlines) into a process's
# argv, which is visible to every other local user via `ps`/`/proc` and is
# fragile to quote around when it is long. Writing it to a file and pointing
# Claude at it sidesteps both: the CLI argument stays short and fixed, and the
# brief's content can be anything without escaping.
_READ_BRIEF_PROMPT = (
    "Read .claude/kestrel-brief.md in this worktree - it is your task brief - and carry it out."
)

# Sent before the exit sequence: interrupts whatever Claude is mid-way through
# (a running tool, a spinner) so `/exit` lands on a responsive prompt rather
# than being swallowed as input to something else.
_INTERRUPT = b"\x03"
_EXIT_COMMAND = b"/exit\r"
STOP_POLL_INTERVAL = 0.2


@dataclass(frozen=True)
class ClaudeCodeConfig:
    projects: dict[str, Path]
    worktrees_root: Path
    server_url: str = "http://127.0.0.1:8099"
    data_dir: Path | None = None
    claude_binary: str = "claude"
    claude_base_args: tuple[str, ...] = ("--dangerously-skip-permissions",)
    stop_timeout: float = 10.0


class NoProjectConfigured(RuntimeError):
    """Raised when a task's project cannot be resolved to a repo path - a
    config problem, not a task problem, so it is worth its own exception
    rather than folding into a generic ValueError."""


@dataclass
class ClaudeCodeExecutor:
    kind: ExecutorKind = field(default=ExecutorKind.CLAUDE_CODE, init=False)

    config: ClaudeCodeConfig
    log: EventLog
    terminals: SessionHostClient
    human_activity: HumanActivityTracker

    # task_id -> session-host terminal id. Rebuilt from the session host on
    # demand rather than persisted - the terminal itself is the durable
    # record, this is just a cache of where to find it.
    _sessions: dict[str, str] = field(default_factory=dict)

    # --- project / worktree --------------------------------------------------

    def _project_for(self, task: Task) -> str:
        """Which configured repo a task's worktree comes from.

        There is no dedicated "project" field on Task (see tasks.py) - `scope`
        is the closest fit, and is reused for it here. With exactly one
        project configured (the common case for a single-user box working one
        repo at a time) that also works unscoped, so tasks do not have to name
        it. Decision: revisit if a task ever needs a scope string that is not
        a project name.
        """
        if task.scope and task.scope in self.config.projects:
            return task.scope
        if len(self.config.projects) == 1:
            return next(iter(self.config.projects))
        raise NoProjectConfigured(
            f"{task.handle}: cannot determine which project this belongs to - "
            f"set task.scope to one of {sorted(self.config.projects)}"
        )

    def _worktree_for(self, task: Task) -> Path:
        project = self._project_for(task)
        repo_path = self.config.projects[project]
        return ensure_worktree(repo_path, self.config.worktrees_root, task.handle)

    # --- Executor protocol -----------------------------------------------------

    async def start(self, task: Task, brief: str) -> None:
        worktree = self._worktree_for(task)
        write_brief(worktree, brief)
        write_hooks_settings(worktree)

        existing = [t for t in await self.terminals.list(task_id=task.id) if t.alive]
        if existing:
            # Restart path: the executor process was rebuilt (server restart,
            # or the orchestrator recreating it), but the session-host PTY -
            # and the `claude` process inside it - never stopped. Reattaching
            # is correct; starting a second `claude` in the same worktree is
            # not.
            self._sessions[task.id] = existing[0].id
            return

        command = [self.config.claude_binary, *self.config.claude_base_args, _READ_BRIEF_PROMPT]
        env = {"KESTREL_TASK_ID": task.handle, "KESTREL_SERVER_URL": self.config.server_url}
        if self.config.data_dir is not None:
            env["KESTREL_DATA"] = str(self.config.data_dir)

        terminal = await self.terminals.create(
            cwd=worktree, command=command, task_id=task.id, env=env
        )
        self._sessions[task.id] = terminal.id
        self.log.append(
            EventKind.SESSION_STARTED,
            "kestrel",
            {"terminal_id": terminal.id, "worktree": str(worktree)},
            task_id=task.id,
        )

    async def send(self, task: Task, message: str) -> Outcome:
        terminal_id = await self._terminal_id_for(task)
        if terminal_id is None:
            return Refused(reason=f"{task.handle} has no active session to send into")

        if self.human_activity.active(terminal_id):
            # Not an error - a human at the keyboard outranks a nudge. The
            # caller (the orchestrator) treats this as "not delivered" rather
            # than counting it as a nudge sent.
            self.log.append(
                EventKind.SEND_DEFERRED,
                "kestrel",
                {"terminal_id": terminal_id, "reason": "human active in session"},
                task_id=task.id,
            )
            return Refused(reason=f"deferred: a human is active in {task.handle}'s session")

        terminal = await self.terminals.get(terminal_id)
        if terminal is None or not terminal.alive:
            return Refused(reason=f"{task.handle}'s session is not running")

        await terminal.write(message.encode() + b"\r")
        return Ok()

    async def stop(self, task: Task, reason: str) -> None:
        terminal_id = self._sessions.pop(task.id, None)
        if terminal_id is None:
            terminal_id = await self._terminal_id_for(task)
        if terminal_id is None:
            return

        terminal = await self.terminals.get(terminal_id)
        if terminal is not None and terminal.alive:
            # Interrupt, then ask nicely, then - only after giving it a
            # chance to shut down cleanly - kill the process group outright.
            # Killing is the last rung, not the first (§6).
            await terminal.write(_INTERRUPT)
            await asyncio.sleep(0.2)
            await terminal.write(_EXIT_COMMAND)
            await self._wait_until_dead(terminal_id, self.config.stop_timeout)

        await self.terminals.close(terminal_id)
        self.log.append(
            EventKind.SESSION_STOPPED,
            "kestrel",
            {"terminal_id": terminal_id, "reason": reason},
            task_id=task.id,
        )

    # --- helpers ---------------------------------------------------------------

    async def _terminal_id_for(self, task: Task) -> str | None:
        cached = self._sessions.get(task.id)
        if cached is not None:
            return cached
        existing = [t for t in await self.terminals.list(task_id=task.id) if t.alive]
        if not existing:
            return None
        self._sessions[task.id] = existing[0].id
        return existing[0].id

    async def _wait_until_dead(self, terminal_id: str, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            terminal = await self.terminals.get(terminal_id)
            if terminal is None or not terminal.alive:
                return
            await asyncio.sleep(STOP_POLL_INTERVAL)

    # --- supervision hooks ------------------------------------------------------
    # Everything below is duck-typed, not part of the `Executor` protocol - the
    # other three executor kinds have no worktree and no PTY to ask about, so
    # the orchestrator calls these only when `hasattr` says they exist.

    async def alive(self, task: Task) -> bool | None:
        """`None` means "no session known" (never started, or already torn
        down) - not a stall, just nothing to judge. `False` is the one that
        matters: a terminal the executor knows about whose process has ended
        without `stop()` ever being called is a session that died mid-task,
        and that is Stuck immediately, not a candidate for the nudge ladder.
        """
        cached = self._sessions.get(task.id)
        if cached is None:
            existing = await self.terminals.list(task_id=task.id)
            if not existing:
                return None
            cached = existing[0].id
        terminal = await self.terminals.get(cached)
        if terminal is None:
            return None
        return terminal.alive

    async def progress_hash(self, task: Task) -> str | None:
        """A hash of the worktree's uncommitted state (tracked changes plus
        untracked files), used as the progress proxy: unchanged for ~15
        minutes while the session is active means nothing is actually
        happening, whatever the transcript looks like. `None` when the task
        has no worktree yet (e.g. the project cannot be resolved)."""
        try:
            worktree = self._worktree_for(task)
        except NoProjectConfigured:
            return None
        if not worktree.exists():
            return None
        return await asyncio.to_thread(_worktree_diff_hash, worktree)

    async def diff(self, task: Task) -> Diff:
        """The task's diff, for claim validation (supervision.py) - a
        completion or blocker claim is checked against this, never trusted."""
        try:
            worktree = self._worktree_for(task)
        except NoProjectConfigured:
            return Diff(added=[], removed=[], files=[])
        if not worktree.exists():
            return Diff(added=[], removed=[], files=[])
        return await asyncio.to_thread(_worktree_diff, worktree)

    async def symbol_exists(self, task: Task, symbol: str) -> bool:
        """Whether `symbol` appears anywhere in the worktree's tracked
        content right now - what a stated blocker is checked against before
        it is accepted as real (`supervision.validate_blocker`)."""
        try:
            worktree = self._worktree_for(task)
        except NoProjectConfigured:
            return False
        if not worktree.exists():
            return False
        return await asyncio.to_thread(_worktree_has_symbol, worktree, symbol)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _worktree_diff_hash(worktree: Path) -> str:
    diff = _git("diff", "HEAD", cwd=worktree).stdout
    untracked = _git("status", "--porcelain", "--untracked-files=all", cwd=worktree).stdout
    return hashlib.sha256((diff + untracked).encode()).hexdigest()


def _worktree_diff(worktree: Path) -> Diff:
    """Tracked changes (`git diff HEAD`) plus untracked files, in the shape
    `supervision.py`'s claim checks already expect: added/removed lines and
    the touched file list. Untracked files show up in `files` with no line
    content - there is nothing to diff against for a file that never existed
    before, but its path still matters for the spec-changes check."""
    numstat = _git("diff", "HEAD", "--numstat", cwd=worktree).stdout
    files = [line.split("\t")[-1] for line in numstat.splitlines() if line.strip()]

    added: list[str] = []
    removed: list[str] = []
    patch = _git("diff", "HEAD", "--no-color", cwd=worktree).stdout
    for line in patch.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])

    status = _git("status", "--porcelain", "--untracked-files=all", cwd=worktree).stdout
    for line in status.splitlines():
        if line.startswith("??"):
            files.append(line[3:].strip())

    return Diff(added=added, removed=removed, files=files)


def _worktree_has_symbol(worktree: Path, symbol: str) -> bool:
    result = _git("grep", "-q", "-F", symbol, "--", ".", cwd=worktree)
    # `git grep` exits 1 for "not found", which is a normal outcome here, not
    # a failure - only treat genuine errors (missing repo, bad revision) as
    # "cannot tell", which this collapses into "does not exist".
    return result.returncode == 0
