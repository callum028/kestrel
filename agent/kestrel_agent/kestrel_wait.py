"""`kestrel-wait` - how a task registers "waiting on X" instead of polling it.

Claude must not arm its own watcher: `gh run watch` in a loop, a shell `sleep`
polling a health endpoint, anything that burns turns checking on something
external. That unattended polling loop is the exact overnight failure the rest
of supervision exists to catch (see docs/design.md §6), and telling the model
to poll "responsibly" does not fix it - not polling at all does.

This command POSTs a wait registration to the server's `/waits` endpoint and
exits immediately. The brief tells Claude to run one of these and then end its
turn (see `kestrel_agent.claude_settings.WAIT_INSTRUCTIONS`) rather than
waiting on the process to return anything - there is nothing to wait for here,
the server does the waiting.

Zero dependencies, deliberately, matching `kestrel_hook.py`: this pays its
import cost on every invocation from inside a task worktree, and
`kestrel-agent` has no dependencies to begin with. `urllib` is the one POST
this needs.
"""

from __future__ import annotations

import argparse
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


def _post(url: str, token: str | None, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read() or b"{}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kestrel-wait",
        description="Register a wait with Kestrel instead of polling for it yourself.",
    )
    sub = parser.add_subparsers(dest="kind", required=True)

    ci = sub.add_parser("ci", help="wait on a GitHub Actions run for a branch")
    ci.add_argument("--branch", required=True)
    ci.add_argument("--timeout", type=float, default=None, help="minutes")

    url = sub.add_parser("url", help="wait on an HTTP endpoint returning an expected status")
    url.add_argument("url")
    url.add_argument("--expect", type=int, default=200)
    url.add_argument("--timeout", type=float, default=None, help="minutes")

    deadline = sub.add_parser("deadline", help="a plain timer - check back at a fixed point")
    deadline.add_argument("--minutes", type=float, required=True)
    deadline.add_argument("--reason", required=True)

    return parser


def _params(args: argparse.Namespace) -> tuple[str, dict, float | None]:
    if args.kind == "ci":
        return "ci", {"branch": args.branch}, args.timeout
    if args.kind == "url":
        return "url", {"url": args.url, "expect": args.expect}, args.timeout
    if args.kind == "deadline":
        return "deadline", {"reason": args.reason}, args.minutes
    raise ValueError(f"unknown wait kind: {args.kind}")  # argparse prevents this


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    kind, params, timeout_minutes = _params(args)

    server_url = os.environ.get("KESTREL_SERVER_URL", DEFAULT_SERVER_URL)
    data_dir = Path(os.environ.get("KESTREL_DATA", str(Path.home() / ".kestrel")))
    task_handle = os.environ.get("KESTREL_TASK_ID")
    if not task_handle:
        print(
            "kestrel-wait: KESTREL_TASK_ID is not set - not running inside a task", file=sys.stderr
        )
        return 1

    token = _token(data_dir)
    payload = {
        "task_handle": task_handle,
        "kind": kind,
        "params": params,
        "timeout_minutes": timeout_minutes,
    }
    try:
        result = _post(f"{server_url}/waits", token, payload)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"kestrel-wait: could not reach {server_url}: {exc}", file=sys.stderr)
        return 1

    if result.get("status") != "ok":
        print(f"kestrel-wait: refused: {result}", file=sys.stderr)
        return 1

    print("kestrel-wait: registered, Kestrel will follow up - end your turn now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
