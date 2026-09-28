"""Worktree creation and reuse - the restart guarantee this module exists for.

Uses real `git`, against throwaway repos under `tmp_path`. No fake here is
faithful enough: the interesting failure modes (a repo with no remote, an
already-existing worktree directory) are properties of git itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kestrel_agent.worktrees import WorktreeError, ensure_worktree, git_common_dir


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


def test_creates_a_worktree_on_its_own_branch(repo, tmp_path):
    worktrees_root = tmp_path / "worktrees"
    target = ensure_worktree(repo, worktrees_root, "KES-1")

    assert target == worktrees_root / "KES-1"
    assert (target / "README.md").read_text() == "hello\n"
    branch = _git("branch", "--show-current", cwd=target).stdout.strip()
    assert branch == "kestrel/KES-1"


def test_reuses_an_existing_worktree_on_restart(repo, tmp_path):
    worktrees_root = tmp_path / "worktrees"
    first = ensure_worktree(repo, worktrees_root, "KES-1")
    (first / "scratch.txt").write_text("still here")

    second = ensure_worktree(repo, worktrees_root, "KES-1")

    assert second == first
    assert (second / "scratch.txt").read_text() == "still here"
    # Reuse must not have run `git worktree add` again - listing worktrees
    # from the main repo shows exactly one entry beyond the main worktree.
    listing = _git("worktree", "list", cwd=repo).stdout.strip().splitlines()
    assert len(listing) == 2


def test_tolerates_a_repo_with_no_remote(repo, tmp_path):
    # No `git remote add` anywhere in the fixture - this must not try (and
    # fail) to fetch.
    target = ensure_worktree(repo, tmp_path / "worktrees", "KES-2")
    assert target.exists()


def test_different_handles_get_independent_worktrees_and_branches(repo, tmp_path):
    worktrees_root = tmp_path / "worktrees"
    a = ensure_worktree(repo, worktrees_root, "KES-1")
    b = ensure_worktree(repo, worktrees_root, "KES-2")
    assert a != b
    assert _git("branch", "--show-current", cwd=a).stdout.strip() == "kestrel/KES-1"
    assert _git("branch", "--show-current", cwd=b).stdout.strip() == "kestrel/KES-2"


def test_git_common_dir_resolves_to_the_main_repos_git_directory(repo, tmp_path):
    worktree = ensure_worktree(repo, tmp_path / "worktrees", "KES-1")
    assert git_common_dir(worktree) == repo / ".git"


def test_a_genuinely_broken_repo_raises(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with pytest.raises(WorktreeError):
        ensure_worktree(not_a_repo, tmp_path / "worktrees", "KES-1")
