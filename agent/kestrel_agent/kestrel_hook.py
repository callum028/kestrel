"""`kestrel-hook` - the command Claude Code's own hooks invoke.

Runs once per hook firing, as a child of the `claude` process, inside the
task's worktree. It reads the hook's JSON off stdin - the contract documented
at code.claude.com/docs/hooks - and forwards it to the server's existing
`/hooks/claude` endpoint, so a PreToolUse call and a Stop enter the event log
the same way regardless of which fired.

On `SessionStart` it also calls `/sessions/bind` first, using the task handle
carried in `$KESTREL_TASK_ID` (set on the PTY's environment when the executor
started this session - see `executors/claude_code.py`). That is what lets
`/hooks/claude` resolve `session_id -> task_id` for every hook after this one;
without the bind, every event from this session would fall through as
`HOOK_UNATTRIBUTED`.

Zero dependencies, deliberately: `kestrel-agent` has none, and this script
pays its import cost on every single tool call a session makes. `urllib` from
the standard library is the one POST this needs.

A hook command's exit code is meaningful to Claude Code - nonzero blocks most
events. Supervision must never be the reason a tool call fails, so every
failure path here is swallowed after being reported to stderr, and the
process always exits 0.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_SERVER_URL = "http://127.0.0.1:8099"
TIMEOUT_SECONDS = 5


def _token(data_dir: Path) -> str | None:
    token_path = data_dir / "token"
    if not token_path.exists():
        return None
    return token_path.read_text().strip()


def _post(url: str, token: str | None, payload: dict) -> None:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        response.read()


def main(argv: list[str] | None = None) -> int:
    raw = sys.stdin.read()
    try:
        hook = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        print("kestrel-hook: malformed hook payload on stdin", file=sys.stderr)
        return 0

    server_url = os.environ.get("KESTREL_SERVER_URL", DEFAULT_SERVER_URL)
    data_dir = Path(os.environ.get("KESTREL_DATA", str(Path.home() / ".kestrel")))
    token = _token(data_dir)
    task_handle = os.environ.get("KESTREL_TASK_ID")
    session_id = hook.get("session_id")

    try:
        if task_handle and session_id and hook.get("hook_event_name") == "SessionStart":
            _post(
                f"{server_url}/sessions/bind",
                token,
                {"session_id": session_id, "task_handle": task_handle},
            )
        _post(f"{server_url}/hooks/claude", token, hook)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"kestrel-hook: could not reach {server_url}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
