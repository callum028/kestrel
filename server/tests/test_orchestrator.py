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
from kestrel.orchestrator import Orchestrator
from kestrel.supervision import MAX_NUDGES
from kestrel.tasks import TaskState

NIGHT = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)


class FakeClaude:
    """Records what it was told rather than running anything."""

    kind = ExecutorKind.CLAUDE_CODE

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.stopped: list[str] = []
        self.started: list[str] = []

    async def start(self, task, brief):
        self.started.append(task.handle)

    async def send(self, task, message):
        self.sent.append(message)

    async def stop(self, task, reason):
        self.stopped.append(reason)


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
