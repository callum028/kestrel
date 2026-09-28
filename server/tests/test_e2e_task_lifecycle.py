"""End-to-end: "pick up KES-1" to done, through every real boundary that a
single-process test can drive.

Real pieces, all at once:

- A real `uvicorn` instance of the actual FastAPI app (`kestrel.api.create_app`),
  reached over a real socket by every other piece below - not `TestClient`.
- A real `python -m kestrel_agent` session host subprocess (`session_host`
  fixture, conftest.py), reached over its real Unix socket.
- A real `ClaudeCodeExecutor` driving a real git worktree, with a FAKE `claude`
  binary standing in for the interactive session - a small script that fires
  the same hooks a real session fires (via the real `kestrel-hook` command,
  itself POSTing to the real app above) and does real git operations (commit,
  push).
- A real `BrainResponder`/`BrainRunner` calling a FAKE headless `claude -p`
  binary that does a REAL MCP round trip: it reads `--mcp-config`, spawns the
  real `kestrel-mcp` server (`kestrel.brain.mcp_server`, unchanged) over stdio
  using the real `mcp` client SDK, and calls `start_task` for real.
- `FakeGitHub` and the default `FakeBoard` (no Notion configured) - the two
  places nothing beyond this process's own memory stands in for a real
  external service. Both are fakes tests are already meant to drive directly
  (see their own docstrings) - this is the sanctioned seam, not a shortcut.
- A real `ValidationRunner`, running a genuinely trivial shell command.

What's simulated rather than driven by a fake service: GitHub itself. There is
no fake `gh`/GitHub HTTP server here - `FakeGitHub.prs`/`.ci`/`.merge_sha` are
poked directly, the same way test_orchestrator.py and test_validation.py
already do, and "merging" the PR is done for real against the same bare repo
(a fast-forward of `main` to the branch's own commit - the fake session never
makes more than one commit, so there is nothing to actually merge). This is
the seam noted in the task brief: building a fake `gh`/GitHub API server was
out of scope for what a single integration test should own, and the
orchestrator's own contract with GitHub is already covered unit-by-unit
elsewhere (test_validation.py, test_orchestrator.py) - what this test adds is
everything *around* that boundary being genuinely wired together.

Along the way this surfaced two real bugs, both fixed separately (see their
own commits) rather than worked around here:

- POST /tasks (and start_task) left a freshly created task at CREATED with
  nothing to ever brief/start it.
- Claim validation diffed a worktree against bare HEAD, which is empty the
  moment a session has committed everything - exactly what a session that
  commits and pushes before its turn ends (the realistic path to a PR) does.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn

from kestrel.api import create_app
from kestrel.config import Config
from kestrel.events import EventKind
from kestrel.github import CIState, CIStatus, FakeGitHub, PullRequest
from kestrel.runtime import Runtime
from kestrel.tasks import TaskState
from kestrel.validation import DeployCheck, ProjectValidation, ValidationCommand

# --- the fake `claude` interactive session (executor side) ------------------
# Same shape as test_claude_code_executor.py's FAKE_CLAUDE, extended to do a
# real commit and push before firing Stop - the realistic "done, PR opened"
# path this test exists to exercise.
FAKE_CLAUDE_EXECUTOR = """
import json
import os
import subprocess
import sys

handle = os.environ.get("KESTREL_TASK_ID", "unknown")
session_id = handle + "-session"


def fire_hook(event, **extra):
    payload = {"hook_event_name": event, "session_id": session_id, **extra}
    subprocess.run(["kestrel-hook"], input=json.dumps(payload).encode(), check=False)


def git(*args):
    subprocess.run(["git", *args], check=True)


fire_hook("SessionStart")

with open(".claude/kestrel-brief.md") as f:
    brief = f.read()
assert brief  # actually read it, even though the fix below is fixed content

with open("CHANGE.md", "w") as f:
    f.write("Addressed the brief for " + handle + ".\\n")
git("add", "CHANGE.md")
git("commit", "-m", handle + ": address the brief")
git("push", "origin", "HEAD:kestrel/" + handle)

fire_hook("Stop", last_assistant_message="Opened a PR for " + handle + ". All tests pass.")

for line in sys.stdin:
    if line.rstrip("\\n") == "/exit":
        break
"""

# --- the fake headless `claude -p` (brain side) ------------------------------
# Parses the real CLI shape BrainRunner.run() builds (runner.py), does a real
# MCP round trip over stdio to the real kestrel-mcp server, and prints
# --output-format json's own shape. Handles two prompt shapes: narration's
# ("write one short Kestrel-voiced message...", narration.py's _prompt) and
# the conversation responder's (asks it to act on Callum's message).
FAKE_CLAUDE_BRAIN = """
import asyncio
import json
import re
import sys


async def main():
    argv = sys.argv[1:]
    mcp_config_path = argv[argv.index("--mcp-config") + 1]
    user_message = argv[-1]

    reply = "Nothing to do."

    if "kestrel-voiced message" in user_message.lower():
        # Narration: a fixed, recognisable transform - never the deterministic
        # fallback template - so the test can tell a real brain call happened.
        facts_line = [l for l in user_message.splitlines() if l.strip().startswith("-")]
        reply = "[narrated] " + "; ".join(f.strip("- ").strip() for f in facts_line)
    else:
        match = re.search(r"KES-\\d+", user_message)
        if "pick up" in user_message.lower() and match:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            with open(mcp_config_path) as f:
                spec = json.load(f)["mcpServers"]["kestrel"]
            params = StdioServerParameters(
                command=spec["command"], args=spec.get("args", []), env=spec.get("env")
            )
            handle = match.group(0)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.call_tool("start_task", {"handle": handle})
            reply = "Started " + handle + "."

    print(json.dumps({"result": reply}))


asyncio.run(main())
"""


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(interval)


@pytest.fixture
def bare_origin(tmp_path) -> Path:
    """Stands in for GitHub's own copy of the repo - a real bare repo, so
    `git push`/`fetch` from the task's worktree are genuine, not mocked."""
    path = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(path)], check=True)
    return path


@pytest.fixture
def repo(tmp_path, bare_origin) -> Path:
    """The project's own working checkout - what `KESTREL_CLAUDE_PROJECTS`
    points at. Has `origin` configured for real, matching a real deployment,
    so `ensure_worktree`/`prepare_validation`'s own `git fetch origin` calls
    are genuine."""
    path = tmp_path / "repo"
    _git("clone", "-q", str(bare_origin), str(path), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    _git("push", "-q", "origin", "main", cwd=path)
    return path


@pytest.fixture
def fake_claude_executor(tmp_path) -> Path:
    path = tmp_path / "fake_claude_executor.py"
    path.write_text(FAKE_CLAUDE_EXECUTOR)
    return path


@pytest.fixture
def fake_claude_brain(tmp_path) -> Path:
    path = tmp_path / "fake_claude_brain.py"
    path.write_text(FAKE_CLAUDE_BRAIN)
    return path


@pytest.fixture
async def app_context(session_host, tmp_path, repo, fake_claude_executor, fake_claude_brain):
    """Everything real, wired together: config, a real Runtime (real
    ClaudeCodeExecutor + real brain responder, both pointed at fakes only at
    the `claude` binary boundary), a real uvicorn instance of the actual app,
    and a `FakeGitHub` for the one boundary this test drives directly rather
    than standing up a fake HTTP service for."""
    port = _free_port()
    server_url = f"http://127.0.0.1:{port}"

    validation = {
        "proj": ProjectValidation(
            deploy=DeployCheck(),  # unconfigured - nothing to confirm before validating
            command=ValidationCommand(command="true", report_path="report.json"),
        )
    }

    config = Config(
        # `data_dir=tmp_path` (not a subdirectory) so this Config's
        # `session_host_socket` (`data_dir / "session-host.sock"`) lands on
        # exactly the socket the `session_host` fixture already started at,
        # the same convention test_claude_code_executor.py's `_config` uses.
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        memory_repo=tmp_path / "memory",
        identity_dir=tmp_path / "identity",
        claude_projects={"proj": repo},
        claude_worktrees_root=tmp_path / "worktrees",
        claude_binary=sys.executable,
        claude_base_args=(str(fake_claude_executor),),
        server_url=server_url,
        brain_claude_binary=sys.executable,
        brain_claude_base_args=(str(fake_claude_brain),),
        validation=validation,
    )
    (config.identity_dir).mkdir(parents=True, exist_ok=True)
    (config.identity_dir / "identity.md").write_text("You are Kestrel, Callum's assistant.")
    (config.identity_dir / "examples.md").write_text("> Done: KES-1 merged.")

    github = FakeGitHub()
    # `executors` is left for `Runtime.build` to auto-register (it does so
    # whenever `config.claude_projects` is set and no override is given) -
    # that is the real, unmodified wiring path a production deployment goes
    # through too, using the fake-`claude`-pointing config built above.
    rt = Runtime.build(config, github=github)

    app = create_app(config=config, runtime=rt)
    server_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_config)
    serve_task = asyncio.create_task(server.serve())
    async with asyncio.timeout(5):
        while not server.started:
            await asyncio.sleep(0.02)

    client = httpx.AsyncClient(
        base_url=server_url, headers={"Authorization": f"Bearer {app.state.token}"}
    )

    yield rt, client, github, repo

    await client.aclose()
    server.should_exit = True
    await serve_task


async def test_pick_up_to_done_through_every_real_boundary(app_context):
    rt, client, github, repo = app_context

    # --- 1. "pick up KES-1" -> the brain (fake) calls start_task for real ---
    posted = await client.post("/conversation/messages", json={"text": "pick up KES-1"})
    assert posted.json()["status"] == "ok"

    await rt.conversation.wait_idle()

    task = rt.tasks.by_handle("KES-1")
    assert task is not None, "the fake brain's start_task call never landed"

    # --- 2. the executor started a real session, which did real git work ---
    await _wait_until(
        lambda: any(e.kind is EventKind.CLAIM_ACCEPTED for e in rt.log.for_task(task.id))
    )
    assert rt.tasks.get(task.id).state is TaskState.RUNNING

    worktree = rt.config.worktrees_root / "KES-1"
    assert (worktree / "CHANGE.md").exists()
    branch = "kestrel/KES-1"
    branch_sha = _git("rev-parse", branch, cwd=worktree).stdout.strip()

    # --- 3. GitHub side, simulated: a PR exists and CI is green -------------
    # (FakeGitHub is a fixture meant to be driven directly - see its own
    # docstring in github.py - the same pattern test_validation.py already
    # uses for the landing/validation gate.)
    github.prs[branch] = PullRequest(
        number=1, url="https://github.com/x/y/pull/1", branch=branch, state="open", merged=False
    )
    now = task.created_at
    # PR detection isn't one of TickReport's own tracked categories (it's
    # neither a stall response nor a landing/validation outcome) - the task's
    # own state is the signal here, not report.quiet.
    await rt.tick_once(now=now)
    assert rt.tasks.get(task.id).state is TaskState.AWAITING_DEV

    github.ci[branch] = CIStatus(CIState.SUCCESS, "all green")

    # --- 4. the merge, done for real against the bare repo ------------------
    # A fast-forward of `main` to the branch's own tip - the fake session
    # never makes more than the one commit, so there is nothing to actually
    # merge, only to land. `merge_sha` has to be a real, fetchable commit for
    # prepare_validation's own `git checkout --force` to succeed below.
    _git("fetch", "origin", cwd=repo)
    _git("push", "origin", f"{branch_sha}:refs/heads/main", cwd=repo)
    github.merge_sha = branch_sha

    # --- 5. merge + validation: one tick both merges (_check_landings) and,
    # since nothing needs to wait a further tick (no deploy check configured),
    # validates in the same pass (_check_validations runs right after
    # _check_landings within the same tick) - so `merged` and `validated`
    # both land here rather than one per call.
    now = now + timedelta(seconds=1)
    report = await rt.tick_once(now=now)

    assert report.merged == ["KES-1"]
    assert report.validated == ["KES-1"]
    final = rt.tasks.get(task.id)
    assert final.state is TaskState.DONE

    # --- 6. the board followed the task to Done ------------------------------
    assert final.ticket_ref is not None
    ticket = rt.board.tickets[final.ticket_ref]
    assert ticket.lane == "Done"

    # --- 7. a narrated "finished" delivery exists, not the raw fallback -----
    deliveries = rt.deliveries.pending()
    finished = [d for d in deliveries if d.task_id == task.id and "finished" in d.subject]
    assert finished, "no finished delivery was recorded"
    assert finished[-1].body.startswith("[narrated]"), (
        f"delivery body was not narrated by the (fake) brain: {finished[-1].body!r}"
    )

    # --- 8. the dev lock was released, not left held -------------------------
    assert rt.dev_lock.holder() is None
