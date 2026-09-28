"""The overnight run, end to end.

Five tickets queued, the first done, the second stuck in a polling loop. This is
the scenario that cost nine hours; here it costs one tick and one sentence.
"""

from datetime import UTC, datetime, timedelta

import pytest

from kestrel.attention import Channel, Signals, Urgency, compute
from kestrel.delivery import DeliveryTracker
from kestrel.devlock import DevLock
from kestrel.events import EventKind
from kestrel.executors import ExecutorKind
from kestrel.github import CIState, CIStatus, FakeGitHub, PullRequest
from kestrel.orchestrator import Orchestrator
from kestrel.outcomes import Ok, Refused
from kestrel.supervision import MAX_NUDGES
from kestrel.tasks import TaskState
from kestrel.waits import WaitKind, WaitStore

NIGHT = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)


class FakeClaude:
    """Records what it was told rather than running anything."""

    kind = ExecutorKind.CLAUDE_CODE

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.stopped: list[str] = []
        self.started: list[str] = []
        self.alive_value: bool | None = True
        # Set to a reason string to make the next `send()` come back Refused,
        # the way a real executor does when a human is active in the session.
        self.refuse_send: str | None = None

    async def start(self, task, brief):
        self.started.append(task.handle)

    async def send(self, task, message):
        if self.refuse_send is not None:
            return Refused(reason=self.refuse_send)
        self.sent.append(message)
        return Ok()

    async def stop(self, task, reason):
        self.stopped.append(reason)

    async def alive(self, task):
        return self.alive_value


@pytest.fixture
def claude():
    return FakeClaude()


@pytest.fixture
def orchestrator(tasks, log, conn, claude):
    return Orchestrator(
        tasks=tasks,
        log=log,
        deliveries=DeliveryTracker(conn, log),
        dev_lock=DevLock(conn),
        executors={ExecutorKind.CLAUDE_CODE: claude},
    )


@pytest.fixture
def asleep():
    return compute(Signals(now=NIGHT, heartbeat_at=NIGHT - timedelta(seconds=5), app_open=False))


def running(tasks, handle="KES-32"):
    t = tasks.create(handle, "Backlog sweep", ["npm test passes"], ExecutorKind.CLAUDE_CODE)
    tasks.transition(t.id, TaskState.BRIEFED)
    tasks.transition(t.id, TaskState.RUNNING)
    tasks.record_progress(t.id, "hash-a")
    return tasks.get(t.id)


def poll(log, task_id, n=40):
    for _ in range(n):
        log.append(EventKind.TOOL_CALL, "claude", {"tool": "gh run watch"}, task_id=task_id)


async def test_a_healthy_task_is_left_alone(orchestrator, tasks, asleep, claude):
    running(tasks)
    report = await orchestrator.tick(NIGHT, asleep)
    assert report.quiet
    assert claude.sent == []


async def test_the_polling_loop_gets_one_sentence(orchestrator, tasks, log, asleep, claude):
    t = running(tasks)
    poll(log, t.id)

    report = await orchestrator.tick(NIGHT, asleep)

    assert report.nudged == ["KES-32"]
    assert len(claude.sent) == 1
    assert "gh run watch" in claude.sent[0]
    assert tasks.get(t.id).state is TaskState.RUNNING  # nudged, not killed


async def test_the_ladder_climbs_before_it_parks(orchestrator, tasks, log, asleep, claude):
    t = running(tasks)
    poll(log, t.id)

    for _ in range(MAX_NUDGES):
        await orchestrator.tick(NIGHT, asleep)

    assert len(claude.sent) == MAX_NUDGES
    assert "Change approach" in claude.sent[-1]

    await orchestrator.tick(NIGHT, asleep)  # restart rung
    assert claude.started == ["KES-32"]

    report = await orchestrator.tick(NIGHT, asleep)  # park rung
    assert report.parked == ["KES-32"]
    assert tasks.get(t.id).state is TaskState.PARKED


async def test_parking_is_reported_but_does_not_ring_him(orchestrator, tasks, log, conn, asleep):
    t = running(tasks)
    poll(log, t.id)
    for _ in range(MAX_NUDGES + 2):
        await orchestrator.tick(NIGHT, asleep)

    assert tasks.get(t.id).state is TaskState.PARKED

    # Parking at 2am is not an emergency: the system already handled it by
    # moving on. Waking to four done and one parked beats being woken.
    rows = conn.execute("SELECT channel, urgency FROM deliveries").fetchall()
    assert rows
    assert all(r["urgency"] != Urgency.BLOCKING for r in rows)
    assert all(r["channel"] != Channel.CALL for r in rows)


async def test_progress_between_ticks_resets_the_ladder(orchestrator, tasks, log, asleep, claude):
    t = running(tasks)
    poll(log, t.id)
    await orchestrator.tick(NIGHT, asleep)
    assert tasks.get(t.id).nudges == 1

    tasks.record_progress(t.id, "hash-b")  # it carried on

    assert tasks.get(t.id).nudges == 0


async def test_a_stuck_deploy_is_surfaced_with_what_is_behind_it(orchestrator, tasks, conn, asleep):
    lock = DevLock(conn)
    lock.acquire("t1", NIGHT)
    lock.acquire("t2", NIGHT)

    report = await orchestrator.tick(NIGHT + timedelta(minutes=40), asleep)

    assert report.stuck_deploy == "t1"
    body = conn.execute("SELECT body FROM deliveries").fetchone()["body"]
    assert "1 task(s) queued behind it" in body


# --- bug fix: a deferred send must not count as a nudge (item 4) -----------


async def test_a_nudge_held_back_by_a_human_typing_is_not_counted(
    orchestrator, tasks, log, conn, asleep, claude
):
    t = running(tasks)
    poll(log, t.id)
    claude.refuse_send = "deferred: a human is active in KES-32's session"

    report = await orchestrator.tick(NIGHT, asleep)

    assert report.nudged == []
    assert report.held_back == ["KES-32"]
    assert tasks.get(t.id).nudges == 0  # the ladder did not move
    assert claude.sent == []  # never actually written into the session

    body = conn.execute("SELECT body FROM deliveries").fetchone()["body"]
    assert "you're already in" in body


async def test_a_nudge_that_fails_for_another_reason_is_not_silently_counted(
    orchestrator, tasks, log, asleep, claude
):
    t = running(tasks)
    poll(log, t.id)
    claude.refuse_send = "KES-32's session is not running"

    report = await orchestrator.tick(NIGHT, asleep)

    assert report.nudged == []
    assert report.held_back == []
    assert tasks.get(t.id).nudges == 0


# --- process death: Stuck immediately, no ladder (item 3) -------------------


async def test_a_dead_session_process_is_parked_immediately_not_nudged(
    orchestrator, tasks, asleep, claude
):
    t = running(tasks)
    claude.alive_value = False

    report = await orchestrator.tick(NIGHT, asleep)

    assert report.parked == ["KES-32"]
    assert report.nudged == []
    assert tasks.get(t.id).state is TaskState.PARKED
    assert claude.sent == []  # no point nudging a process that is not there


async def test_a_session_with_no_known_terminal_is_not_treated_as_dead(
    orchestrator, tasks, asleep, claude
):
    """`alive() is None` means "nothing to judge" (e.g. never started through
    this executor instance) - not a stall by itself."""
    running(tasks)
    claude.alive_value = None

    report = await orchestrator.tick(NIGHT, asleep)

    assert report.parked == []


# --- GitHub: PR detection, CI gate, merge-on-green (item 2) -----------------


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def waits(conn, log):
    return WaitStore(conn, log)


@pytest.fixture
def landing_orchestrator(tasks, log, conn, claude, github, waits):
    return Orchestrator(
        tasks=tasks,
        log=log,
        deliveries=DeliveryTracker(conn, log),
        dev_lock=DevLock(conn),
        executors={ExecutorKind.CLAUDE_CODE: claude},
        waits=waits,
        github=github,
    )


def _pr(number=7, branch="kestrel/KES-32", merged=False):
    return PullRequest(
        number=number,
        url=f"https://github.com/x/y/pull/{number}",
        branch=branch,
        state="open",
        merged=merged,
    )


async def test_a_detected_pr_moves_the_task_to_awaiting_dev(
    landing_orchestrator, tasks, github, asleep
):
    t = running(tasks)
    github.prs["kestrel/KES-32"] = _pr()

    await landing_orchestrator.tick(NIGHT, asleep)

    assert tasks.get(t.id).state is TaskState.AWAITING_DEV


async def test_green_ci_merges_and_moves_to_validating(
    landing_orchestrator, tasks, github, conn, asleep
):
    t = running(tasks)
    github.prs["kestrel/KES-32"] = _pr()
    await landing_orchestrator.tick(NIGHT, asleep)  # detects the PR -> AWAITING_DEV

    github.ci["kestrel/KES-32"] = CIStatus(CIState.SUCCESS, "all green")
    report = await landing_orchestrator.tick(NIGHT, asleep)

    assert report.merged == ["KES-32"]
    assert github.merged == [7]
    # This fixture has no project validation configured, so the same tick's
    # validation pass (see test_validation.py for the configured case) walks
    # it straight through to DONE and releases the dev lock - the fix for
    # "nothing ever releases the dev lock", not a second bug.
    assert tasks.get(t.id).state is TaskState.DONE
    assert DevLock(conn).holder() is None


async def test_red_ci_parks_the_task_instead_of_merging(
    landing_orchestrator, tasks, github, asleep
):
    t = running(tasks)
    github.prs["kestrel/KES-32"] = _pr()
    await landing_orchestrator.tick(NIGHT, asleep)

    github.ci["kestrel/KES-32"] = CIStatus(CIState.FAILURE, "CI failed at lint")
    report = await landing_orchestrator.tick(NIGHT, asleep)

    assert report.merged == []
    assert github.merged == []
    assert tasks.get(t.id).state is TaskState.PARKED


async def test_pending_ci_neither_merges_nor_parks(landing_orchestrator, tasks, github, asleep):
    t = running(tasks)
    github.prs["kestrel/KES-32"] = _pr()
    await landing_orchestrator.tick(NIGHT, asleep)

    github.ci["kestrel/KES-32"] = CIStatus(CIState.PENDING, "still running: test")
    report = await landing_orchestrator.tick(NIGHT, asleep)

    assert report.merged == []
    assert report.parked == []
    assert tasks.get(t.id).state is TaskState.AWAITING_DEV


async def test_merge_refusal_parks_and_releases_the_lock(
    landing_orchestrator, tasks, github, conn, asleep
):
    t = running(tasks)
    github.prs["kestrel/KES-32"] = _pr()
    await landing_orchestrator.tick(NIGHT, asleep)
    github.ci["kestrel/KES-32"] = CIStatus(CIState.SUCCESS, "green")
    github.refuse_merge = "merge conflict"

    report = await landing_orchestrator.tick(NIGHT, asleep)

    assert report.merged == []
    assert tasks.get(t.id).state is TaskState.PARKED
    assert DevLock(conn).holder() is None  # not left held for nothing


# --- Kestrel-owned waits: wake on resolution, escalate on expiry (item 1) ---


async def test_a_resolved_ci_wait_wakes_the_session(
    landing_orchestrator, tasks, waits, github, asleep, claude
):
    t = running(tasks)
    waits.register(t.id, WaitKind.CI, {"branch": "kestrel/KES-32"}, NIGHT + timedelta(minutes=30))
    github.ci["kestrel/KES-32"] = CIStatus(CIState.FAILURE, "CI run 4821 failed at step lint")

    report = await landing_orchestrator.tick(NIGHT, asleep)

    assert report.woken == ["KES-32"]
    assert any("failed at step lint" in m for m in claude.sent)
    assert waits.active_for_task(t.id) == []


async def test_a_pending_ci_wait_does_not_wake_or_nudge(
    landing_orchestrator, tasks, waits, github, asleep, claude
):
    t = running(tasks)
    waits.register(t.id, WaitKind.CI, {"branch": "kestrel/KES-32"}, NIGHT + timedelta(minutes=30))
    github.ci["kestrel/KES-32"] = CIStatus(CIState.PENDING, "still running")

    report = await landing_orchestrator.tick(NIGHT, asleep)

    assert report.woken == []
    assert report.nudged == []  # the pending wait suppresses the no-progress/idle nudge
    assert claude.sent == []


async def test_an_expired_wait_escalates_as_needs_you_with_the_facts(
    landing_orchestrator, tasks, waits, github, conn, asleep
):
    t = running(tasks)
    waits.register(t.id, WaitKind.CI, {"branch": "kestrel/KES-32"}, NIGHT - timedelta(minutes=1))
    github.ci["kestrel/KES-32"] = CIStatus(CIState.PENDING, "still running: test")

    report = await landing_orchestrator.tick(NIGHT, asleep)

    assert report.escalated == ["KES-32"]
    assert tasks.get(t.id).state is TaskState.NEEDS_INPUT
    body = conn.execute("SELECT body FROM deliveries ORDER BY sent_at DESC LIMIT 1").fetchone()[
        "body"
    ]
    assert "never resolved" in body
