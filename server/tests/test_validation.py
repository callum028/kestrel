"""Validation - Kestrel's own authoritative run, after merge.

Design §5 step 7: green CI gates the merge, it is not the same as done. These
tests cover the seam pieces directly (`ValidationRunner`,
`parse_playwright_report`) with a tiny real script rather than a real
Playwright suite, and the orchestrator's use of them end to end - the dev
lock's lifecycle is the point, not the test framework - with fakes for
GitHub, the executor, and the subprocess.
"""

from __future__ import annotations

import json
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kestrel.attention import Signals, compute
from kestrel.delivery import DeliveryTracker
from kestrel.devlock import DevLock
from kestrel.events import EventKind
from kestrel.executors import ExecutorKind
from kestrel.github import CIState, CIStatus, FakeGitHub, PullRequest
from kestrel.orchestrator import Orchestrator
from kestrel.outcomes import Ok, Refused
from kestrel.tasks import TaskState
from kestrel.validation import (
    DeployCheck,
    FakeHealthChecker,
    ProjectValidation,
    RunOutcome,
    ValidationCommand,
    ValidationRunner,
    parse_playwright_report,
)

NIGHT = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)
ASLEEP = compute(Signals(now=NIGHT, heartbeat_at=NIGHT - timedelta(seconds=5), app_open=False))


class FakeClaude:
    """Same shape as test_orchestrator.py's FakeClaude, plus the two
    validation-only duck-typed hooks (`project_for`, `prepare_validation`)."""

    kind = ExecutorKind.CLAUDE_CODE

    def __init__(self, project: str | None = None, worktree: Path | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.stopped: list[str] = []
        self.alive_value: bool | None = True
        self.prepared: list[tuple[str, str]] = []
        self.worktree = worktree
        self._project = project
        self.refuse_send: str | None = None
        self.rebranched: list[tuple[str, str]] = []
        self.rebranch_result: str | None = "kestrel/KES-40-fix-1"

    async def start(self, task, brief):
        pass

    async def send(self, task, message):
        if self.refuse_send is not None:
            return Refused(reason=self.refuse_send)
        self.sent.append((task.handle, message))
        return Ok()

    async def stop(self, task, reason):
        self.stopped.append(task.handle)

    async def alive(self, task):
        return self.alive_value

    def project_for(self, task):
        return self._project

    async def prepare_validation(self, task, merge_sha):
        self.prepared.append((task.handle, merge_sha))
        return self.worktree

    async def rebranch_for_fix(self, task, merge_sha):
        self.rebranched.append((task.handle, merge_sha))
        return self.rebranch_result


@dataclass
class FakeValidationRunner:
    """Controlled directly by the test rather than exercising a real shell -
    the real subprocess/report-parsing path is covered separately below by
    `TestRealValidationRunner`."""

    result: RunOutcome = field(default_factory=lambda: RunOutcome(ok=True, summary="ok"))
    calls: list[tuple[str, Path]] = field(default_factory=list)
    raise_exc: Exception | None = None

    async def run(self, command: ValidationCommand, cwd: Path, dev_url: str | None) -> RunOutcome:
        self.calls.append((command.command, cwd))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


def _validating_task(tasks, log, github, handle="KES-40", pr_number=9, sha="deadbeef" * 5):
    t = tasks.create(
        handle, "Ship the feature", ["Playwright suite passes"], ExecutorKind.CLAUDE_CODE
    )
    tasks.transition(t.id, TaskState.BRIEFED)
    tasks.transition(t.id, TaskState.RUNNING)
    branch = f"kestrel/{handle}"
    github.prs[branch] = PullRequest(
        number=pr_number,
        url=f"https://github.com/x/y/pull/{pr_number}",
        branch=branch,
        state="closed",
        merged=True,
    )
    tasks.transition(t.id, TaskState.AWAITING_DEV, reason="PR opened")
    log.append(
        EventKind.PR_MERGED,
        "kestrel",
        {"pr_number": pr_number, "pr_url": github.prs[branch].url, "sha": sha},
        task_id=t.id,
    )
    tasks.transition(t.id, TaskState.VALIDATING, reason="merged")
    return tasks.get(t.id), sha


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def health():
    return FakeHealthChecker()


def make_orchestrator(
    tasks, log, conn, claude, github, *, validation=None, runner=None, health=None
):
    return Orchestrator(
        tasks=tasks,
        log=log,
        deliveries=DeliveryTracker(conn, log),
        dev_lock=DevLock(conn),
        executors={ExecutorKind.CLAUDE_CODE: claude},
        github=github,
        validation=validation or {},
        validation_runner=runner,
        health_checker=health,
    )


# --- no validation configured: visible pass-through, lock released --------


async def test_no_validation_configured_passes_through_and_releases_the_lock(
    tasks, log, conn, github
):
    claude = FakeClaude()
    t, _ = _validating_task(tasks, log, github)
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)
    orc = make_orchestrator(tasks, log, conn, claude, github)

    report = await orc.tick(NIGHT, ASLEEP)

    assert report.validated == ["KES-40"]
    assert tasks.get(t.id).state is TaskState.DONE
    assert lock.holder() is None
    body = conn.execute("SELECT body FROM deliveries ORDER BY sent_at DESC LIMIT 1").fetchone()[
        "body"
    ]
    assert "no validation configured" in body


# --- deploy confirmation: success, failure, timeout ------------------------


def _project_validation(**overrides) -> ProjectValidation:
    defaults = {
        "deploy": DeployCheck(workflow="deploy.yml", timeout=timedelta(minutes=15)),
        "command": ValidationCommand(command="true", report_path="report.json"),
    }
    defaults.update(overrides)
    return ProjectValidation(**defaults)


async def test_deploy_confirmed_by_workflow_run_then_validation_runs(
    tasks, log, conn, github, tmp_path
):
    claude = FakeClaude(project="repo", worktree=tmp_path)
    t, sha = _validating_task(tasks, log, github)
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)
    github.workflow_runs[("deploy.yml", sha)] = CIStatus(CIState.SUCCESS, "deployed")
    runner = FakeValidationRunner(result=RunOutcome(ok=True, summary="12 test(s) passed"))
    orc = make_orchestrator(
        tasks, log, conn, claude, github, validation={"repo": _project_validation()}, runner=runner
    )

    report = await orc.tick(NIGHT, ASLEEP)

    assert claude.prepared == [("KES-40", sha)]
    assert runner.calls  # the command actually ran
    assert report.validated == ["KES-40"]
    assert tasks.get(t.id).state is TaskState.DONE
    assert lock.holder() is None


async def test_deploy_workflow_failure_reopens_the_task(tasks, log, conn, github, tmp_path):
    claude = FakeClaude(project="repo", worktree=tmp_path)
    t, sha = _validating_task(tasks, log, github)
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)
    github.workflow_runs[("deploy.yml", sha)] = CIStatus(CIState.FAILURE, "deploy job failed")
    runner = FakeValidationRunner()
    orc = make_orchestrator(
        tasks, log, conn, claude, github, validation={"repo": _project_validation()}, runner=runner
    )

    report = await orc.tick(NIGHT, ASLEEP)

    assert not runner.calls  # never got to the authoritative run
    assert report.reopened == ["KES-40"]
    assert tasks.get(t.id).state is TaskState.RUNNING
    assert lock.holder() is None
    assert any("did not confirm" in m or "deploy" in m for _h, m in claude.sent)


async def test_deploy_never_confirming_times_out_and_reopens(tasks, log, conn, github, tmp_path):
    claude = FakeClaude(project="repo", worktree=tmp_path)
    t, _sha = _validating_task(tasks, log, github)
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)
    # No workflow run recorded at all (None forever) - just silence.
    runner = FakeValidationRunner()
    orc = make_orchestrator(
        tasks,
        log,
        conn,
        claude,
        github,
        validation={
            "repo": _project_validation(
                deploy=DeployCheck(workflow="deploy.yml", timeout=timedelta(minutes=15))
            )
        },
        runner=runner,
    )

    # Within the deadline: still pending, nothing acted on.
    report = await orc.tick(NIGHT + timedelta(minutes=5), ASLEEP)
    assert report.reopened == []
    assert report.parked == []
    assert tasks.get(t.id).state is TaskState.VALIDATING
    assert lock.holder() is not None

    # Past the deadline: treated as a deploy failure.
    report = await orc.tick(NIGHT + timedelta(minutes=20), ASLEEP)
    assert report.reopened == ["KES-40"]
    assert tasks.get(t.id).state is TaskState.RUNNING
    assert lock.holder() is None


async def test_deploy_confirmed_by_url_when_no_workflow_configured(
    tasks, log, conn, github, health, tmp_path
):
    claude = FakeClaude(project="repo", worktree=tmp_path)
    t, sha = _validating_task(tasks, log, github)
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)
    health.ready[("https://dev.example/health", sha)] = True
    config = _project_validation(
        deploy=DeployCheck(
            url="https://dev.example/health", url_match=sha, timeout=timedelta(minutes=10)
        )
    )
    runner = FakeValidationRunner(result=RunOutcome(ok=True, summary="ok"))
    orc = make_orchestrator(
        tasks, log, conn, claude, github, validation={"repo": config}, runner=runner, health=health
    )

    report = await orc.tick(NIGHT, ASLEEP)

    assert report.validated == ["KES-40"]
    assert tasks.get(t.id).state is TaskState.DONE


# --- the authoritative run: pass / fail / crash -----------------------------


async def test_validation_failure_reopens_with_evidence_and_releases_the_lock(
    tasks, log, conn, github, tmp_path
):
    claude = FakeClaude(project="repo", worktree=tmp_path)
    t, sha = _validating_task(tasks, log, github)
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)
    github.workflow_runs[("deploy.yml", sha)] = CIStatus(CIState.SUCCESS, "deployed")
    runner = FakeValidationRunner(
        result=RunOutcome(
            ok=False,
            summary="2 test(s) failed",
            failing_tests=["checkout > pays with card", "login > shows error on bad password"],
            output_tail="Error: timeout waiting for selector",
        )
    )
    orc = make_orchestrator(
        tasks, log, conn, claude, github, validation={"repo": _project_validation()}, runner=runner
    )

    report = await orc.tick(NIGHT, ASLEEP)

    assert report.reopened == ["KES-40"]
    assert tasks.get(t.id).state is TaskState.RUNNING
    assert lock.holder() is None
    handle, message = claude.sent[-1]
    assert handle == "KES-40"
    assert "checkout > pays with card" in message
    assert "timeout waiting for selector" in message
    assert "kestrel/KES-40-fix-1" in message

    # The worktree was moved off the detached merge commit onto the fresh
    # branch, not left there - that's the whole point of the rebranch.
    assert claude.rebranched == [("KES-40", sha)]


async def test_validation_failure_with_session_gone_parks_instead(
    tasks, log, conn, github, tmp_path
):
    claude = FakeClaude(project="repo", worktree=tmp_path)
    claude.alive_value = False
    t, sha = _validating_task(tasks, log, github)
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)
    github.workflow_runs[("deploy.yml", sha)] = CIStatus(CIState.SUCCESS, "deployed")
    runner = FakeValidationRunner(result=RunOutcome(ok=False, summary="1 test(s) failed"))
    orc = make_orchestrator(
        tasks, log, conn, claude, github, validation={"repo": _project_validation()}, runner=runner
    )

    report = await orc.tick(NIGHT, ASLEEP)

    assert report.parked == ["KES-40"]
    assert tasks.get(t.id).state is TaskState.PARKED
    assert lock.holder() is None
    assert claude.stopped == ["KES-40"]


async def test_validation_failure_reopen_survives_a_failed_rebranch(
    tasks, log, conn, github, tmp_path
):
    """The rebranch is best-effort - a failed `git checkout -B` must not stop
    the evidence reaching Claude, it just means the message stays without the
    fresh-branch pointer."""
    claude = FakeClaude(project="repo", worktree=tmp_path)
    claude.rebranch_result = None
    t, sha = _validating_task(tasks, log, github)
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)
    github.workflow_runs[("deploy.yml", sha)] = CIStatus(CIState.SUCCESS, "deployed")
    runner = FakeValidationRunner(result=RunOutcome(ok=False, summary="1 test(s) failed"))
    orc = make_orchestrator(
        tasks, log, conn, claude, github, validation={"repo": _project_validation()}, runner=runner
    )

    report = await orc.tick(NIGHT, ASLEEP)

    assert report.reopened == ["KES-40"]
    assert claude.rebranched == [("KES-40", sha)]
    _handle, message = claude.sent[-1]
    assert "fresh branch" not in message


async def test_validation_pass_flags_unrequested_spec_changes(tasks, log, conn, github, tmp_path):
    claude = FakeClaude(project="repo", worktree=tmp_path)
    t, sha = _validating_task(tasks, log, github, pr_number=11)
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)
    github.workflow_runs[("deploy.yml", sha)] = CIStatus(CIState.SUCCESS, "deployed")
    github.files[11] = ["src/checkout.ts", "tests/checkout.spec.ts"]
    runner = FakeValidationRunner(result=RunOutcome(ok=True, summary="9 test(s) passed"))
    orc = make_orchestrator(
        tasks, log, conn, claude, github, validation={"repo": _project_validation()}, runner=runner
    )

    report = await orc.tick(NIGHT, ASLEEP)

    assert report.validated == ["KES-40"]
    body = conn.execute("SELECT body FROM deliveries ORDER BY sent_at DESC LIMIT 1").fetchone()[
        "body"
    ]
    assert "tests/checkout.spec.ts" in body


async def test_a_crashing_validation_run_still_releases_the_lock(
    tasks, log, conn, github, tmp_path
):
    claude = FakeClaude(project="repo", worktree=tmp_path)
    t, sha = _validating_task(tasks, log, github)
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)
    github.workflow_runs[("deploy.yml", sha)] = CIStatus(CIState.SUCCESS, "deployed")
    runner = FakeValidationRunner(raise_exc=RuntimeError("boom"))
    orc = make_orchestrator(
        tasks, log, conn, claude, github, validation={"repo": _project_validation()}, runner=runner
    )

    report = await orc.tick(NIGHT, ASLEEP)

    assert report.parked == ["KES-40"]
    assert tasks.get(t.id).state is TaskState.PARKED
    assert lock.holder() is None


async def test_no_worktree_to_validate_against_parks_and_releases_the_lock(
    tasks, log, conn, github
):
    claude = FakeClaude(project="repo", worktree=None)  # prepare_validation returns None
    claude.alive_value = False  # nothing left to notify inline - this is a park, not a reopen
    t, sha = _validating_task(tasks, log, github)
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)
    github.workflow_runs[("deploy.yml", sha)] = CIStatus(CIState.SUCCESS, "deployed")
    runner = FakeValidationRunner()
    orc = make_orchestrator(
        tasks, log, conn, claude, github, validation={"repo": _project_validation()}, runner=runner
    )

    report = await orc.tick(NIGHT, ASLEEP)

    assert not runner.calls
    assert report.parked == ["KES-40"]
    assert lock.holder() is None


# --- a second task queues behind the lock, then proceeds -------------------


async def test_a_second_task_queues_behind_validation_then_proceeds(
    tasks, log, conn, github, tmp_path
):
    claude = FakeClaude(project="repo", worktree=tmp_path)
    t1, sha1 = _validating_task(tasks, log, github, handle="KES-40", pr_number=9, sha="a" * 40)

    # A second task, still running, whose PR is green and ready to merge -
    # exactly the scenario the dev lock exists for (design §5's "the dev
    # environment is a singleton").
    t2 = tasks.create("KES-41", "Second feature", ["works"], ExecutorKind.CLAUDE_CODE)
    tasks.transition(t2.id, TaskState.BRIEFED)
    tasks.transition(t2.id, TaskState.RUNNING)
    github.prs["kestrel/KES-41"] = PullRequest(
        number=12,
        url="https://github.com/x/y/pull/12",
        branch="kestrel/KES-41",
        state="open",
        merged=False,
    )
    github.ci["kestrel/KES-41"] = CIStatus(CIState.SUCCESS, "green")

    lock = DevLock(conn)
    lock.acquire(t1.id, NIGHT)
    github.workflow_runs[("deploy.yml", sha1)] = CIStatus(CIState.SUCCESS, "deployed")
    runner = FakeValidationRunner(result=RunOutcome(ok=True, summary="ok"))
    orc = make_orchestrator(
        tasks, log, conn, claude, github, validation={"repo": _project_validation()}, runner=runner
    )

    # First tick: t1 validates and finishes; t2's PR is detected (RUNNING ->
    # AWAITING_DEV) but the merge itself only happens once the lock is free.
    await orc.tick(NIGHT, ASLEEP)
    assert tasks.get(t1.id).state is TaskState.DONE
    assert lock.holder() is None

    # Second tick: the lock is free, t2 acquires it and merges.
    report = await orc.tick(NIGHT, ASLEEP)
    assert report.merged == ["KES-41"] or tasks.get(t2.id).state in (
        TaskState.VALIDATING,
        TaskState.DONE,
    )


# --- rebase note to in-flight sessions on the same repo ---------------------


async def test_landing_notifies_other_running_sessions_to_rebase(tasks, log, conn, github):
    claude = FakeClaude(project="repo")

    t1 = tasks.create("KES-50", "First", ["a"], ExecutorKind.CLAUDE_CODE)
    tasks.transition(t1.id, TaskState.BRIEFED)
    tasks.transition(t1.id, TaskState.RUNNING)
    github.prs["kestrel/KES-50"] = PullRequest(
        number=20,
        url="https://github.com/x/y/pull/20",
        branch="kestrel/KES-50",
        state="open",
        merged=False,
    )
    github.ci["kestrel/KES-50"] = CIStatus(CIState.SUCCESS, "green")

    t2 = tasks.create("KES-51", "Second, still in flight", ["b"], ExecutorKind.CLAUDE_CODE)
    tasks.transition(t2.id, TaskState.BRIEFED)
    tasks.transition(t2.id, TaskState.RUNNING)

    orc = make_orchestrator(tasks, log, conn, claude, github)  # no validation - t1 finishes fast

    await orc.tick(NIGHT, ASLEEP)  # detects t1's PR -> AWAITING_DEV
    await orc.tick(NIGHT, ASLEEP)  # merges t1, notifies t2

    handles_sent = {h for h, _m in claude.sent}
    assert "KES-51" in handles_sent
    rebase_messages = [m for h, m in claude.sent if h == "KES-51"]
    assert any("rebase" in m for m in rebase_messages)


# --- URL wait: resolve and expire -------------------------------------------


async def test_url_wait_resolves_when_the_health_check_passes(tasks, log, conn, github, health):
    from kestrel.waits import WaitKind, WaitStore

    claude = FakeClaude()
    waits = WaitStore(conn, log)
    t = tasks.create("KES-60", "Waiting on a URL", ["works"], ExecutorKind.CLAUDE_CODE)
    tasks.transition(t.id, TaskState.BRIEFED)
    tasks.transition(t.id, TaskState.RUNNING)
    waits.register(
        t.id,
        WaitKind.URL,
        {"url": "https://dev.example/status", "expect": 200},
        NIGHT + timedelta(minutes=30),
    )
    health.ready[("https://dev.example/status", None)] = True
    orc = Orchestrator(
        tasks=tasks,
        log=log,
        deliveries=DeliveryTracker(conn, log),
        dev_lock=DevLock(conn),
        executors={ExecutorKind.CLAUDE_CODE: claude},
        waits=waits,
        github=github,
        health_checker=health,
    )

    report = await orc.tick(NIGHT, ASLEEP)

    assert report.woken == ["KES-60"]
    assert waits.active_for_task(t.id) == []
    assert any("200" in m for _h, m in claude.sent)


async def test_url_wait_expires_when_the_health_check_never_passes(
    tasks, log, conn, github, health
):
    from kestrel.waits import WaitKind, WaitStore

    claude = FakeClaude()
    waits = WaitStore(conn, log)
    t = tasks.create(
        "KES-61", "Waiting on a URL that never comes up", ["works"], ExecutorKind.CLAUDE_CODE
    )
    tasks.transition(t.id, TaskState.BRIEFED)
    tasks.transition(t.id, TaskState.RUNNING)
    waits.register(
        t.id,
        WaitKind.URL,
        {"url": "https://dev.example/status", "expect": 200},
        NIGHT - timedelta(minutes=1),
    )
    orc = Orchestrator(
        tasks=tasks,
        log=log,
        deliveries=DeliveryTracker(conn, log),
        dev_lock=DevLock(conn),
        executors={ExecutorKind.CLAUDE_CODE: claude},
        waits=waits,
        github=github,
        health_checker=health,
    )

    report = await orc.tick(NIGHT, ASLEEP)

    assert report.escalated == ["KES-61"]
    assert tasks.get(t.id).state is TaskState.NEEDS_INPUT


# --- ValidationRunner + parse_playwright_report: the real seam -------------


def _write_report(path: Path, *, failed: int, failing_titles: list[str]) -> None:
    tests = [
        {
            "title": title,
            "results": [{"status": "failed" if failed else "passed"}],
        }
        for title in failing_titles
    ] or [{"title": "a passing test", "results": [{"status": "passed"}]}]
    report = {
        "stats": {"expected": 1 if not failed else 0, "unexpected": failed},
        "suites": [{"title": "suite", "specs": [{"title": "spec", "tests": tests}]}],
    }
    path.write_text(json.dumps(report))


async def test_validation_runner_reports_success_from_exit_code_and_report(tmp_path):
    report_path = tmp_path / "report.json"
    _write_report(report_path, failed=0, failing_titles=[])
    script = tmp_path / "run.py"
    script.write_text("import sys; sys.exit(0)")
    command = ValidationCommand(
        command=f"{sys.executable} {script}",
        report_path="report.json",
        timeout=timedelta(seconds=10),
    )
    runner = ValidationRunner()

    outcome = await runner.run(command, tmp_path, dev_url=None)

    assert outcome.ok
    assert "passed" in outcome.summary


async def test_validation_runner_reports_failure_and_failing_tests(tmp_path):
    report_path = tmp_path / "report.json"
    _write_report(report_path, failed=1, failing_titles=["checkout > pays with card"])
    script = tmp_path / "run.py"
    script.write_text("import sys; sys.exit(1)")
    command = ValidationCommand(
        command=f"{sys.executable} {script}",
        report_path="report.json",
        timeout=timedelta(seconds=10),
    )
    runner = ValidationRunner()

    outcome = await runner.run(command, tmp_path, dev_url=None)

    assert not outcome.ok
    assert any("checkout > pays with card" in t for t in outcome.failing_tests)


async def test_validation_runner_times_out_a_hanging_command(tmp_path):
    script = tmp_path / "run.py"
    script.write_text("import time; time.sleep(30)")
    command = ValidationCommand(
        command=f"{sys.executable} {script}",
        report_path="report.json",
        timeout=timedelta(seconds=0.2),
    )
    runner = ValidationRunner()

    outcome = await runner.run(command, tmp_path, dev_url=None)

    assert not outcome.ok
    assert outcome.timed_out


async def test_validation_runner_passes_the_dev_url_through_the_env(tmp_path):
    script = tmp_path / "run.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, sys
            sys.exit(0 if os.environ.get("KESTREL_DEV_URL") == "https://dev.example" else 1)
            """
        )
    )
    command = ValidationCommand(
        command=f"{sys.executable} {script}",
        report_path="report.json",
        timeout=timedelta(seconds=10),
    )
    runner = ValidationRunner()

    outcome = await runner.run(command, tmp_path, dev_url="https://dev.example")

    assert outcome.ok


def test_parse_playwright_report_missing_file_returns_none(tmp_path):
    assert parse_playwright_report(tmp_path / "missing.json") is None


def test_parse_playwright_report_reads_stats_and_failing_titles(tmp_path):
    path = tmp_path / "report.json"
    _write_report(path, failed=1, failing_titles=["login > shows error"])

    summary = parse_playwright_report(path)

    assert summary is not None
    assert summary.failed == 1
    assert summary.failing_tests == ["suite > spec > login > shows error"]
