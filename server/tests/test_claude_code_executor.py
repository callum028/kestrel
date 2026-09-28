"""The Claude Code executor, end to end.

`session_host` (conftest.py) is a real `python -m kestrel_agent` subprocess -
the property under test throughout this suite is the real boundary between
the executor and the session it drives, so an in-process PTY stand-in would
paper over exactly what matters. `claude` itself is faked: a tiny script that
fires the same hooks a real session fires (via the real `kestrel-hook`
command) and echoes whatever is written to it, run against a real `uvicorn`
instance of the actual app so the hook round trip is genuine end to end.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn

from kestrel.api import create_app
from kestrel.config import Config
from kestrel.events import EventKind
from kestrel.executors.claude_code import ClaudeCodeConfig, ClaudeCodeExecutor
from kestrel.outcomes import Ok, Refused
from kestrel.runtime import Runtime
from kestrel.tasks import TaskState

FAKE_CLAUDE = """
import json
import os
import subprocess
import sys

session_id = os.environ.get("KESTREL_TASK_ID", "unknown") + "-session"


def fire_hook(event, **extra):
    payload = {"hook_event_name": event, "session_id": session_id, **extra}
    subprocess.run(["kestrel-hook"], input=json.dumps(payload).encode(), check=False)


fire_hook("SessionStart")
print("brief-prompt:" + sys.argv[-1], flush=True)

for line in sys.stdin:
    line = line.rstrip("\\n")
    print("echo:" + line, flush=True)
    if line == "/exit":
        fire_hook("Stop")
        break
"""


async def drain_until(queue: asyncio.Queue, needle: bytes, timeout: float = 5.0) -> bytes:
    buffer = b""
    async with asyncio.timeout(timeout):
        while needle not in buffer:
            chunk = await queue.get()
            if chunk is None:
                break
            buffer += chunk
    return buffer


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


@pytest.fixture
def fake_claude(tmp_path) -> Path:
    path = tmp_path / "fake_claude.py"
    path.write_text(FAKE_CLAUDE)
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        memory_repo=tmp_path / "memory",
        identity_dir=tmp_path / "identity",
    )


@pytest.fixture
async def live(session_host, tmp_path, repo, fake_claude):
    """A real Runtime, a real uvicorn instance of the app in front of it (so
    `kestrel-hook` has something genuine to POST to), and a ClaudeCodeExecutor
    wired to both plus the fake `claude` binary. All on one event loop, so
    the SessionHostClient's asyncio primitives are never touched from two
    loops at once.
    """
    config = _config(tmp_path)
    rt = Runtime.build(config)
    app = create_app(config=config, runtime=rt)

    port = _free_port()
    server_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_config)
    serve_task = asyncio.create_task(server.serve())
    async with asyncio.timeout(5):
        while not server.started:
            await asyncio.sleep(0.02)

    executor = ClaudeCodeExecutor(
        config=ClaudeCodeConfig(
            projects={"proj": repo},
            worktrees_root=tmp_path / "worktrees",
            server_url=f"http://127.0.0.1:{port}",
            data_dir=config.data_dir,
            claude_binary=sys.executable,
            claude_base_args=(str(fake_claude),),
        ),
        log=rt.log,
        terminals=rt.terminals,
        human_activity=rt.human_activity,
    )

    yield rt, executor

    server.should_exit = True
    await serve_task


async def test_start_creates_worktree_pty_and_events_flow_to_the_server(live, tmp_path):
    rt, executor = live
    task = rt.tasks.create("KES-1", "Fix the bug", ["tests pass"], "claude_code", scope="proj")

    await executor.start(task, brief="Do the thing.\nMind the `backticks`.")

    worktree = tmp_path / "worktrees" / "KES-1"
    assert (worktree / ".claude" / "kestrel-brief.md").read_text().startswith("Do the thing.")
    assert json.loads((worktree / ".claude" / "settings.local.json").read_text())["hooks"]

    terminals = await rt.terminals.list(task_id=task.id)
    assert len(terminals) == 1
    assert terminals[0].alive is True

    started = [e for e in rt.log.for_task(task.id) if e.kind is EventKind.SESSION_STARTED]
    assert len(started) == 1

    # SessionStart -> kestrel-hook -> /sessions/bind, round-tripped for real.
    await wait_until(lambda: rt.task_for_session(f"{task.handle}-session") == task.id)
    bound = [e for e in rt.log.for_task(task.id) if e.kind is EventKind.SESSION_BOUND]
    assert len(bound) == 1


async def test_restart_reuses_the_worktree_and_the_running_session(live, tmp_path):
    rt, executor = live
    task = rt.tasks.create("KES-2", "Fix the bug", ["tests pass"], "claude_code", scope="proj")
    await executor.start(task, brief="first brief")

    # A brand-new executor instance, as a restarted process would build - it
    # remembers nothing about KES-2.
    restarted = ClaudeCodeExecutor(
        config=executor.config, log=rt.log, terminals=rt.terminals, human_activity=rt.human_activity
    )
    await restarted.start(task, brief="first brief")

    terminals = await rt.terminals.list(task_id=task.id)
    assert len(terminals) == 1  # no second `claude` process spawned

    listing = _git("worktree", "list", cwd=tmp_path / "repo").stdout.strip().splitlines()
    assert len(listing) == 2


async def test_send_writes_when_idle_and_defers_when_a_human_is_active(live, tmp_path):
    rt, executor = live
    task = rt.tasks.create("KES-3", "Fix the bug", ["tests pass"], "claude_code", scope="proj")
    await executor.start(task, brief="brief")

    terminal = (await rt.terminals.list(task_id=task.id))[0]
    queue = await terminal.subscribe()
    await drain_until(queue, b"brief-prompt:")

    outcome = await executor.send(task, "hello")
    assert isinstance(outcome, Ok)
    await drain_until(queue, b"echo:hello")

    rt.human_activity.mark(terminal.id)
    outcome = await executor.send(task, "world")
    assert isinstance(outcome, Refused)
    assert "deferred" in outcome.reason

    # And it really was never written - draining briefly must not see it.
    with pytest.raises(TimeoutError):
        await drain_until(queue, b"echo:world", timeout=0.5)

    await terminal.unsubscribe(queue)


async def test_stop_ends_the_session_and_leaves_the_worktree(live, tmp_path):
    rt, executor = live
    task = rt.tasks.create("KES-4", "Fix the bug", ["tests pass"], "claude_code", scope="proj")
    await executor.start(task, brief="brief")
    terminal_id = (await rt.terminals.list(task_id=task.id))[0].id

    await executor.stop(task, reason="testing stop")

    await wait_until(lambda: True)  # let the close op round-trip
    remaining = await rt.terminals.list(task_id=task.id)
    assert remaining == []

    stopped = [e for e in rt.log.for_task(task.id) if e.kind is EventKind.SESSION_STOPPED]
    assert len(stopped) == 1
    assert stopped[0].payload["reason"] == "testing stop"
    assert stopped[0].payload["terminal_id"] == terminal_id

    assert (tmp_path / "worktrees" / "KES-4").exists()


# --- supervision hooks: progress_hash, diff, symbol_exists, alive -----------
# What the orchestrator (orchestrator.py) calls to wire the stall detector and
# claim validation to the real worktree, rather than requiring the caller to
# tell it what changed.


async def test_progress_hash_is_stable_until_the_worktree_actually_changes(live, tmp_path):
    rt, executor = live
    task = rt.tasks.create("KES-10", "Fix the bug", ["tests pass"], "claude_code", scope="proj")
    await executor.start(task, brief="brief")
    worktree = tmp_path / "worktrees" / "KES-10"

    baseline = await executor.progress_hash(task)
    assert baseline is not None
    assert await executor.progress_hash(task) == baseline

    (worktree / "new_file.txt").write_text("hello\n")
    assert await executor.progress_hash(task) != baseline


async def test_progress_hash_is_none_when_the_project_cannot_be_resolved(live, tmp_path):
    rt, executor = live
    # A second configured project makes the task's project ambiguous without
    # an explicit `scope` - `_project_for` raises `NoProjectConfigured` rather
    # than guessing, and `progress_hash`/`diff`/`symbol_exists` all treat that
    # the same way as "no worktree yet".
    executor.config.projects["other"] = tmp_path / "unused"
    task = rt.tasks.create("KES-11", "Fix the bug", ["tests pass"], "claude_code")  # no scope
    assert await executor.progress_hash(task) is None


async def test_diff_reports_tracked_and_untracked_changes(live, tmp_path):
    rt, executor = live
    task = rt.tasks.create("KES-12", "Fix the bug", ["tests pass"], "claude_code", scope="proj")
    await executor.start(task, brief="brief")
    worktree = tmp_path / "worktrees" / "KES-12"

    (worktree / "README.md").write_text("goodbye\n")
    (worktree / "new_file.txt").write_text("new content\n")

    diff = await executor.diff(task)
    assert "README.md" in diff.files
    assert "new_file.txt" in diff.files
    assert any("goodbye" in line for line in diff.added)
    assert any("hello" in line for line in diff.removed)


async def test_diff_is_empty_for_an_untouched_worktree(live):
    rt, executor = live
    task = rt.tasks.create("KES-13", "Fix the bug", ["tests pass"], "claude_code", scope="proj")
    await executor.start(task, brief="brief")

    diff = await executor.diff(task)
    assert diff.added == diff.removed == diff.files == []


async def test_symbol_exists_checks_the_worktrees_tracked_content(live):
    rt, executor = live
    task = rt.tasks.create("KES-14", "Fix the bug", ["tests pass"], "claude_code", scope="proj")
    await executor.start(task, brief="brief")

    assert await executor.symbol_exists(task, "hello") is True
    assert await executor.symbol_exists(task, "LEGACY_SYNC_FLAG_NOBODY_WROTE") is False


async def test_alive_is_none_for_a_task_the_executor_has_never_started(live):
    rt, executor = live
    task = rt.tasks.create("KES-15", "Fix the bug", ["tests pass"], "claude_code", scope="proj")
    assert await executor.alive(task) is None


async def test_alive_is_true_while_running_and_false_once_the_process_exits_on_its_own(live):
    rt, executor = live
    task = rt.tasks.create("KES-16", "Fix the bug", ["tests pass"], "claude_code", scope="proj")
    await executor.start(task, brief="brief")
    assert await executor.alive(task) is True

    terminal = (await rt.terminals.list(task_id=task.id))[0]
    await terminal.write(b"/exit\r")

    # The process ends on its own here (unlike `executor.stop()`), which is
    # exactly the "died mid-task" case the orchestrator's immediate-park
    # branch exists for.
    async with asyncio.timeout(5):
        while await executor.alive(task) is not False:
            await asyncio.sleep(0.05)


# --- restart reconciliation (item 8) -----------------------------------------
# On startup, a task believed RUNNING is checked against the session host
# rather than assumed healthy - Runtime.reconcile_on_startup, called from
# api.py's lifespan.


async def test_reconciliation_leaves_a_healthy_running_task_alone(live):
    rt, executor = live
    task = rt.tasks.create("KES-20", "Fix the bug", ["tests pass"], "claude_code", scope="proj")
    rt.tasks.transition(task.id, TaskState.BRIEFED)
    rt.tasks.transition(task.id, TaskState.RUNNING)
    await executor.start(task, brief="brief")
    rt.executors["claude_code"] = executor

    unaccountable = await rt.reconcile_on_startup()

    assert unaccountable == []
    assert rt.deliveries.pending() == []


async def test_reconciliation_reports_a_running_task_with_no_live_session(live):
    rt, executor = live
    task = rt.tasks.create("KES-21", "Fix the bug", ["tests pass"], "claude_code", scope="proj")
    rt.tasks.transition(task.id, TaskState.BRIEFED)
    rt.tasks.transition(task.id, TaskState.RUNNING)
    # Never actually started through this executor - nothing on the host
    # accounts for it, e.g. the server restarted and forgot.
    rt.executors["claude_code"] = executor

    unaccountable = await rt.reconcile_on_startup()

    assert unaccountable == ["KES-21"]
    pending = rt.deliveries.pending()
    assert len(pending) == 1
    assert "KES-21" in pending[0].body
