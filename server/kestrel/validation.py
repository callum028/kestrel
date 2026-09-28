"""Validation - Kestrel's own, authoritative post-deploy run.

Green CI gates the merge; it does not mean the work is done (design §5 step
7). There is no localhost, so "done" can only be judged against the shared
dev environment, after it has actually redeployed to the merge commit. Two
things live here, both deliberately outside the session Claude runs in:

- **Deploy confirmation.** Per-project, either a named GitHub Actions deploy
  workflow's run for the merge sha going green, a health/version URL
  reporting that sha (or just 200), or both.
- **The authoritative test run.** A per-project command, executed by Kestrel
  itself - never taken from Claude's own summary - with its exit code and a
  JSON report artefact read directly.

Both an HTTP GET (`HttpChecker`) and the subprocess invocation
(`ValidationRunner`) are behind small seams so tests can substitute a fake
transport / fake process without touching the network or a shell, matching
the project rule that tests never do either for real.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

# How much of a failing run's own output to keep for the evidence sent back to
# the session - enough to be useful, short enough to paste into a message.
OUTPUT_TAIL_LINES = 40


@dataclass(frozen=True)
class DeployCheck:
    """How a project's dev deploy is confirmed for a given merge commit.

    Either or both may be set. The workflow run is checked first when
    present - it is the more precise signal, because a version/health URL
    that has not rolled forward yet still happily returns 200. When only a
    URL is configured, that alone is what "deployed" means for this project.
    """

    workflow: str | None = None  # a named GitHub Actions workflow file/name
    url: str | None = None  # health/version endpoint
    url_match: str | None = None  # substring expected in the body; None -> 200 is enough
    timeout: timedelta = timedelta(minutes=15)

    @property
    def configured(self) -> bool:
        return bool(self.workflow or self.url)


@dataclass(frozen=True)
class ValidationCommand:
    """The authoritative run - same command Claude iterates with, but
    triggered by Kestrel outside the session, its exit code and report read
    directly rather than taken on trust (design §5 step 7)."""

    command: str  # run through the shell, e.g. "npx playwright test"
    report_path: str  # JSON reporter output, relative to the working directory
    timeout: timedelta = timedelta(minutes=20)
    dev_url_env: str = "KESTREL_DEV_URL"  # env var the command reads the base URL from


@dataclass(frozen=True)
class ProjectValidation:
    """Everything configured for one project. `command is None` is the
    explicit "no validation configured" case (design §5 step 7d) - handled as
    a visible pass-through, never a silent skip."""

    deploy: DeployCheck = field(default_factory=DeployCheck)
    command: ValidationCommand | None = None
    dev_url: str | None = None

    @property
    def configured(self) -> bool:
        return self.command is not None


@dataclass(frozen=True)
class ReportSummary:
    passed: int
    failed: int
    failing_tests: list[str]


def parse_playwright_report(path: Path) -> ReportSummary | None:
    """Reads the Playwright JSON reporter's own output - never Claude's
    summary of it. `None` when the file is missing or unparseable, which the
    caller treats as "no evidence", not as a pass."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return None

    failing: list[str] = []

    def walk(suite: dict[str, Any], prefix: str) -> None:
        title = (
            f"{prefix} > {suite.get('title', '')}".strip(" >") if prefix else suite.get("title", "")
        )
        for spec in suite.get("specs", []):
            spec_title = f"{title} > {spec.get('title', '?')}".strip(" >")
            for test in spec.get("tests", []):
                results = test.get("results", [])
                if (
                    any(r.get("status") not in ("passed", "skipped") for r in results)
                    or not results
                ):
                    test_title = test.get("title")
                    full_title = (
                        f"{spec_title} > {test_title}".strip(" >") if test_title else spec_title
                    )
                    failing.append(full_title)
        for child in suite.get("suites", []):
            walk(child, title)

    for suite in data.get("suites", []):
        walk(suite, "")

    stats = data.get("stats", {})
    passed = int(stats.get("expected", 0))
    failed = int(stats.get("unexpected", 0)) or len(failing)
    return ReportSummary(passed=passed, failed=failed, failing_tests=failing)


@runtime_checkable
class HealthChecker(Protocol):
    async def check(self, url: str, expect_status: int, match: str | None) -> bool:
        """True if `url` currently reports the expected status and (when
        given) `match` appears in the body."""
        ...


class HttpHealthChecker:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def check(self, url: str, expect_status: int = 200, match: str | None = None) -> bool:
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError:
            return False
        if resp.status_code != expect_status:
            return False
        if match is None:
            return True
        return match in resp.text


@dataclass
class FakeHealthChecker:
    """Test double - no network. `ready` keys on `(url, match)` so a test can
    make a URL "become" ready once it starts reporting the right sha."""

    ready: dict[tuple[str, str | None], bool] = field(default_factory=dict)

    async def check(self, url: str, expect_status: int = 200, match: str | None = None) -> bool:
        return self.ready.get((url, match), False)


@dataclass(frozen=True)
class RunOutcome:
    ok: bool
    summary: str
    failing_tests: list[str] = field(default_factory=list)
    output_tail: str = ""
    timed_out: bool = False


class ValidationRunner:
    """Runs the authoritative command off the event loop.

    `asyncio.create_subprocess_shell` already does its I/O without blocking
    the loop - other ticks and coroutines keep running while this awaits the
    process - so no thread pool is needed to satisfy "ticks aren't blocked".
    `run_subprocess` is swappable for a fake process in tests that want to
    avoid running a real shell.
    """

    def __init__(self, run_subprocess: Any = None) -> None:
        self._run_subprocess = run_subprocess or asyncio.create_subprocess_shell

    async def run(self, command: ValidationCommand, cwd: Path, dev_url: str | None) -> RunOutcome:
        env = dict(os.environ)
        if dev_url:
            env[command.dev_url_env] = dev_url

        try:
            proc = await self._run_subprocess(
                command.command,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # A new session (process group) so a timeout can kill whatever
                # the shell spawned, not just the shell itself - without this,
                # `proc.kill()` only signals `/bin/sh -c ...`, and any child it
                # forked (e.g. a test runner) is orphaned and keeps running,
                # keeping the stdout pipe open until IT exits naturally. That
                # was a real 30-second-per-run hang in this class's own test
                # for exactly this timeout path.
                start_new_session=True,
            )
        except OSError as exc:
            return RunOutcome(ok=False, summary=f"failed to start validation command: {exc}")

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=command.timeout.total_seconds()
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            minutes = int(command.timeout.total_seconds() // 60)
            return RunOutcome(
                ok=False,
                summary=f"validation timed out after {minutes} minute(s)",
                timed_out=True,
            )

        output = stdout.decode(errors="replace") if stdout else ""
        tail = "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])
        report = parse_playwright_report(cwd / command.report_path)

        if proc.returncode == 0 and (report is None or report.failed == 0):
            summary = f"{report.passed} test(s) passed" if report else "validation command exited 0"
            return RunOutcome(ok=True, summary=summary, output_tail=tail)

        if report is not None:
            summary = f"{report.failed} test(s) failed"
            failing = report.failing_tests
        else:
            summary = f"validation command exited {proc.returncode}"
            failing = []
        return RunOutcome(ok=False, summary=summary, failing_tests=failing, output_tail=tail)
