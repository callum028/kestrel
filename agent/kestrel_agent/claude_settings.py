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
# each one carries on stdin. `PreToolUse` was missing here even though
# `/hooks/claude` has always handled it (api.py's HookIn/claude_hook treat
# Pre- and PostToolUse identically) - without it registered, activity was
# only ever counted on the trailing edge of a tool call, which matters for
# anything long-running.
HOOK_EVENTS = (
    "SessionStart",
    "Stop",
    "Notification",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
)

# Told to Claude via the brief (see `write_brief` below), not the identity
# layer (which is Kestrel's own model, not Claude Code's) - this is the one
# place a task's instructions are assembled before they reach the worktree.
# The point: Claude must never arm its own watcher for something external
# (`gh run watch`, a sleep loop, polling a health endpoint) - that unattended
# polling loop is the overnight failure supervision exists to catch. It
# registers what it is waiting for instead and stops, and Kestrel wakes it
# when the real thing resolves or the deadline passes.
WAIT_INSTRUCTIONS = """
## Waiting on something external

If you need to wait on something outside this session - a CI run, a
deployment becoming healthy, anything you would otherwise poll for - do not
poll it yourself and do not sleep in a loop. Register it and end your turn:

    kestrel-wait ci --branch <branch> [--timeout <minutes>]
    kestrel-wait url <https://...> --expect <status> [--timeout <minutes>]
    kestrel-wait deadline --minutes <n> --reason "<what you're waiting for>"

Kestrel checks the real thing on its own schedule and will send you a message
with the result (or tell you the deadline passed) - you do not need to, and
should not, check it yourself in the meantime.
""".strip()


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
    path.write_text(f"{brief}\n\n{WAIT_INSTRUCTIONS}\n")
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
