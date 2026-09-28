"""Per-worktree Claude Code configuration for Kestrel supervision.

Two files go into every task worktree, and neither may ever reach the task's
own branch:

- `.claude/settings.local.json` wires up the hooks that make the stall
  detector and the question channel real (§6 of the design) - it names a
  `kestrel-hook` command, an absolute-path notion that means nothing outside
  this box.
- The brief file is Kestrel's scratch input, not part of the change Claude is
  making.

Both are excluded via `.git/info/exclude` rather than a tracked `.gitignore`:
editing `.gitignore` is itself a diff Claude could commit, which is exactly
the kind of incidental change the design's "trivial fix" bounds exist to
catch, whereas `info/exclude` is local-only by definition and never enters a
diff at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from .worktrees import git_common_dir

BRIEF_RELATIVE = Path(".claude/kestrel-brief.md")
SETTINGS_RELATIVE = Path(".claude/settings.local.json")

# No matcher on any of these: Kestrel wants every firing of each event, not a
# tool-filtered subset. See code.claude.com/docs/hooks for the payload shape
# each one carries on stdin.
HOOK_EVENTS = ("SessionStart", "Stop", "Notification", "UserPromptSubmit", "PostToolUse")


def hooks_settings(hook_command: str = "kestrel-hook") -> dict:
    entry = {"hooks": [{"type": "command", "command": hook_command}]}
    return {"hooks": {event: [entry] for event in HOOK_EVENTS}}


def write_hooks_settings(worktree: Path, hook_command: str = "kestrel-hook") -> Path:
    path = worktree / SETTINGS_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hooks_settings(hook_command), indent=2) + "\n")
    _exclude(worktree, SETTINGS_RELATIVE)
    return path


def write_brief(worktree: Path, brief: str) -> Path:
    path = worktree / BRIEF_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief)
    _exclude(worktree, BRIEF_RELATIVE)
    return path


def _exclude(worktree: Path, relative: Path) -> None:
    exclude_path = git_common_dir(worktree) / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    line = relative.as_posix()
    existing = exclude_path.read_text() if exclude_path.exists() else ""
    if line in existing.splitlines():
        return
    with exclude_path.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(line + "\n")
