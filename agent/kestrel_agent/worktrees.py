"""Git worktrees, one per task.

Tasks run concurrently (§5 of the design), which only works if each one gets
its own working directory and branch. A linked worktree off the project's
default branch gives that for the price of one `git` command, and the
worktree survives the task's process, the executor's process, and a server
restart - all of which is the point: an agent that died mid-task must be able
to pick the same directory back up rather than losing what was on disk.

Deliberately just `git worktree add` wrapped in idempotency and default-branch
detection - no cleverness, because the failure mode that matters here is
silent duplication (two worktrees for one task handle), not missing a rebase
strategy.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    """A git operation needed to stand up a task's worktree failed outright -
    as opposed to the worktree already existing, which is the expected,
    happy-path case on restart."""


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _default_branch(repo_path: Path) -> str:
    """Best-effort: origin's HEAD if there is a remote, else whichever of
    main/master exists, else whatever is currently checked out. Tests run
    against throwaway repos with no remote, so every rung has to work without
    one."""
    symbolic = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo_path)
    if symbolic.returncode == 0 and symbolic.stdout.strip():
        return symbolic.stdout.strip().rsplit("/", 1)[-1]

    for candidate in ("main", "master"):
        if _run(["git", "rev-parse", "--verify", candidate], repo_path).returncode == 0:
            return candidate

    current = _run(["git", "branch", "--show-current"], repo_path)
    if current.returncode == 0 and current.stdout.strip():
        return current.stdout.strip()

    raise WorktreeError(f"cannot determine a default branch for {repo_path}")


def git_common_dir(worktree: Path) -> Path:
    """The shared `.git` directory behind a worktree - a linked worktree's own
    `.git` is a *file* pointing at `<repo>/.git/worktrees/<name>`, and things
    like `info/exclude` live in the common dir, not per-worktree. Asking git
    directly avoids parsing that pointer file by hand."""
    result = _run(["git", "rev-parse", "--git-common-dir"], worktree)
    if result.returncode != 0:
        raise WorktreeError(
            f"could not resolve the git dir for {worktree}: {result.stderr.strip()}"
        )
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else worktree / path


def ensure_worktree(repo_path: Path, worktrees_root: Path, handle: str) -> Path:
    """Create the worktree for one task, or reuse it if it is already there.

    Reuse is the restart path: the executor is stateless across process
    restarts (nothing about a running task lives in memory anywhere but the
    session host), so "does this directory already exist" has to stand in for
    "have I done this before" without a database of its own.
    """
    repo_path = Path(repo_path)
    worktrees_root = Path(worktrees_root)
    target = worktrees_root / handle

    if (target / ".git").exists():
        return target

    worktrees_root.mkdir(parents=True, exist_ok=True)

    remote = _run(["git", "remote"], repo_path)
    if remote.returncode == 0 and remote.stdout.strip():
        # A fetch failure (offline, flaky network) must not block starting the
        # task - the worktree still branches fine off whatever refs are
        # already local, just possibly stale.
        _run(["git", "fetch", "--all", "--prune"], repo_path)

    branch = _default_branch(repo_path)
    task_branch = f"kestrel/{handle}"
    result = _run(["git", "worktree", "add", str(target), "-b", task_branch, branch], repo_path)
    if result.returncode != 0:
        raise WorktreeError(f"git worktree add failed for {handle}: {result.stderr.strip()}")
    return target
