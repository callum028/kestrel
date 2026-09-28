"""Narration wired into the orchestrator's deliveries.

`brain/narration.py` is tested standalone in `test_brain_narration.py`; this
file covers the seam - that every user-facing delivery the orchestrator sends
(needs-you, finished, something's-wrong) is actually routed through
`Orchestrator.narrate`, with the right `NarrationKind` and the same facts that
used to be inlined directly into the delivery body, and that the deterministic
fallback is exactly what happens when no narrator is configured at all (the
brain off) - matching `runtime._build_narrator` returning `None` in that case.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kestrel.attention import Signals, compute
from kestrel.brain.narration import NarrationKind, NarrationRequest
from kestrel.delivery import DeliveryTracker
from kestrel.devlock import DevLock
from kestrel.events import EventKind
from kestrel.executors import ExecutorKind
from kestrel.github import FakeGitHub, PullRequest
from kestrel.orchestrator import Orchestrator
from kestrel.outcomes import Ok, Refused
from kestrel.supervision import MAX_NUDGES
from kestrel.tasks import TaskState

NIGHT = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)


class FakeClaude:
    kind = ExecutorKind.CLAUDE_CODE

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.stopped: list[str] = []
        self.alive_value: bool | None = True
        self.refuse_send: str | None = None

    async def start(self, task, brief):
        pass

    async def send(self, task, message):
        if self.refuse_send is not None:
            return Refused(reason=self.refuse_send)
        self.sent.append(message)
        return Ok()

    async def stop(self, task, reason):
        self.stopped.append(reason)

    async def alive(self, task):
        return self.alive_value


class RecordingNarrator:
    """Stands in for `runtime._build_narrator`'s callable - records every
    request it was asked to word, and returns a fixed, recognisable string
    rather than anything template-shaped, so a test can tell "the narrator's
    output reached the delivery" apart from "the fallback happened to look
    similar"."""

    def __init__(self) -> None:
        self.requests: list[NarrationRequest] = []

    async def __call__(self, request: NarrationRequest) -> str:
        self.requests.append(request)
        return f"[narrated:{request.kind}] {' / '.join(request.facts)}"


@pytest.fixture
def claude():
    return FakeClaude()


@pytest.fixture
def narrator():
    return RecordingNarrator()


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


async def _park_a_stalled_task(tasks, log, conn, claude, asleep, narrator=None):
    orc = Orchestrator(
        tasks=tasks,
        log=log,
        deliveries=DeliveryTracker(conn, log),
        dev_lock=DevLock(conn),
        executors={ExecutorKind.CLAUDE_CODE: claude},
        narrator=narrator,
    )
    t = running(tasks)
    poll(log, t.id)
    for _ in range(MAX_NUDGES + 2):
        await orc.tick(NIGHT, asleep)
    assert tasks.get(t.id).state is TaskState.PARKED
    return t


async def test_with_no_narrator_configured_the_delivery_is_the_deterministic_fallback(
    tasks, log, conn, claude, asleep
):
    """`narrator=None` is exactly what `runtime._build_narrator` returns when
    the brain is off - this must never be a missing or blank delivery, and it
    must be the fixed template, not some other hand-written string."""
    await _park_a_stalled_task(tasks, log, conn, claude, asleep, narrator=None)

    body = conn.execute("SELECT body FROM deliveries ORDER BY sent_at DESC LIMIT 1").fetchone()[
        "body"
    ]
    assert body.startswith("Something's wrong:")
    assert "KES-32" in body


async def test_parking_is_narrated_as_something_wrong_with_the_stall_facts(
    tasks, log, conn, claude, narrator, asleep
):
    t = await _park_a_stalled_task(tasks, log, conn, claude, asleep, narrator=narrator)

    assert narrator.requests
    park_request = narrator.requests[-1]
    assert park_request.kind is NarrationKind.SOMETHING_WRONG
    assert any(t.handle in f for f in park_request.facts)

    body = conn.execute("SELECT body FROM deliveries ORDER BY sent_at DESC LIMIT 1").fetchone()[
        "body"
    ]
    assert body.startswith("[narrated:something_wrong]")


async def test_finishing_a_task_is_narrated_as_finished(tasks, log, conn, narrator, asleep):
    claude = FakeClaude()
    github = FakeGitHub()
    t = tasks.create("KES-40", "Ship it", [], ExecutorKind.CLAUDE_CODE)
    tasks.transition(t.id, TaskState.BRIEFED)
    tasks.transition(t.id, TaskState.RUNNING)
    branch = "kestrel/KES-40"
    github.prs[branch] = PullRequest(
        number=9, url="https://github.com/x/y/pull/9", branch=branch, state="closed", merged=True
    )
    tasks.transition(t.id, TaskState.AWAITING_DEV, reason="PR opened")
    log.append(
        EventKind.PR_MERGED,
        "kestrel",
        {"pr_number": 9, "pr_url": github.prs[branch].url, "sha": "a" * 40},
        task_id=t.id,
    )
    tasks.transition(t.id, TaskState.VALIDATING, reason="merged")
    lock = DevLock(conn)
    lock.acquire(t.id, NIGHT)

    orc = Orchestrator(
        tasks=tasks,
        log=log,
        deliveries=DeliveryTracker(conn, log),
        dev_lock=lock,
        executors={ExecutorKind.CLAUDE_CODE: claude},
        github=github,
        validation=None,  # nothing configured -> passes straight through
        narrator=narrator,
    )
    report = await orc.tick(NIGHT, asleep)

    assert report.validated == ["KES-40"]
    assert tasks.get(t.id).state is TaskState.DONE
    assert narrator.requests
    finished_request = narrator.requests[-1]
    assert finished_request.kind is NarrationKind.FINISHED
    assert any("KES-40" in f for f in finished_request.facts)

    body = conn.execute("SELECT body FROM deliveries ORDER BY sent_at DESC LIMIT 1").fetchone()[
        "body"
    ]
    assert body.startswith("[narrated:finished]")
