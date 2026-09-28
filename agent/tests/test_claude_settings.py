"""The hooks config and brief file written into every task worktree.

The property that matters most here is exclusion: neither file may ever show
up as untracked-and-committable inside the worktree, because both carry
host-local paths or Kestrel's own scratch input, never something Claude should
be checking in.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kestrel_agent.claude_settings import (
    HOOK_EVENTS,
    WAIT_INSTRUCTIONS,
    hooks_settings,
    write_brief,
    write_hooks_settings,
)
from kestrel_agent.worktrees import ensure_worktree


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def worktree(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return ensure_worktree(repo, tmp_path / "worktrees", "KES-1")


def test_hooks_settings_covers_the_required_events_with_the_hook_command():
    settings = hooks_settings("kestrel-hook")
    assert set(settings["hooks"]) == set(HOOK_EVENTS)
    for event in HOOK_EVENTS:
        [entry] = settings["hooks"][event]
        [hook] = entry["hooks"]
        assert hook == {"type": "command", "command": "kestrel-hook"}


def test_write_hooks_settings_writes_valid_json_into_the_worktree(worktree):
    path = write_hooks_settings(worktree)
    assert path == worktree / ".claude" / "settings.local.json"
    assert json.loads(path.read_text()) == hooks_settings()


def test_write_brief_writes_the_brief_and_the_wait_instructions(worktree):
    brief = 'Fix the thing.\nWatch out for `backticks` and "quotes".\n'
    path = write_brief(worktree, brief)
    assert path == worktree / ".claude" / "kestrel-brief.md"
    written = path.read_text()
    assert written.startswith(brief)
    # Claude must not arm its own watcher - it registers a wait and stops
    # instead of polling, and that instruction lives in every brief.
    assert WAIT_INSTRUCTIONS in written
    assert "kestrel-wait" in written


def test_settings_and_brief_are_excluded_from_the_worktrees_branch(worktree):
    write_hooks_settings(worktree)
    write_brief(worktree, "do the thing")

    status = _git("status", "--porcelain", cwd=worktree).stdout
    assert ".claude/settings.local.json" not in status
    assert ".claude/kestrel-brief.md" not in status


def test_excluding_twice_does_not_duplicate_the_entry(worktree):
    write_hooks_settings(worktree)
    write_hooks_settings(worktree)
    from kestrel_agent.worktrees import git_common_dir

    exclude = (git_common_dir(worktree) / "info" / "exclude").read_text()
    assert exclude.count(".claude/settings.local.json") == 1
