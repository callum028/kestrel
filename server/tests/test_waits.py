"""Kestrel-owned waits - registration and the due/resolved bookkeeping.

Polling the real thing behind a wait is the orchestrator's job (see
test_orchestrator.py); this file only covers the storage and arithmetic.
"""

from datetime import UTC, datetime, timedelta

from kestrel.events import EventKind
from kestrel.executors import ExecutorKind
from kestrel.tasks import TaskState
from kestrel.waits import DEFAULT_TIMEOUT_MINUTES, WaitKind, WaitStore, default_deadline

NOW = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)


def running_task(tasks):
    t = tasks.create("KES-40", "Ship it", ["tests pass"], str(ExecutorKind.CLAUDE_CODE))
    tasks.transition(t.id, TaskState.BRIEFED)
    tasks.transition(t.id, TaskState.RUNNING)
    return tasks.get(t.id)


def test_default_deadline_uses_the_kind_specific_timeout():
    deadline = default_deadline(WaitKind.CI, NOW, None)
    assert deadline == NOW + timedelta(minutes=DEFAULT_TIMEOUT_MINUTES["ci"])


def test_default_deadline_honours_an_explicit_timeout():
    deadline = default_deadline(WaitKind.CI, NOW, 5)
    assert deadline == NOW + timedelta(minutes=5)


def test_register_logs_and_stores_the_wait(tasks, log, conn):
    t = running_task(tasks)
    store = WaitStore(conn, log)
    wait = store.register(
        t.id, WaitKind.CI, {"branch": "kestrel/KES-40"}, NOW + timedelta(minutes=30)
    )

    assert wait.task_id == t.id
    assert wait.kind is WaitKind.CI
    assert wait.params == {"branch": "kestrel/KES-40"}
    assert not wait.resolved

    registered = [e for e in log.for_task(t.id) if e.kind is EventKind.WAIT_REGISTERED]
    assert len(registered) == 1


def test_active_for_task_excludes_resolved_waits(tasks, log, conn):
    t = running_task(tasks)
    store = WaitStore(conn, log)
    wait = store.register(t.id, WaitKind.URL, {"url": "https://x"}, NOW + timedelta(minutes=10))
    assert store.active_for_task(t.id) == [wait]

    store.resolve(wait.id, "200 OK")
    assert store.active_for_task(t.id) == []
    assert store.get(wait.id).result == "200 OK"


def test_due_is_false_before_the_deadline_and_true_after(tasks, log, conn):
    t = running_task(tasks)
    store = WaitStore(conn, log)
    wait = store.register(
        t.id, WaitKind.DEADLINE, {"reason": "check back"}, NOW + timedelta(minutes=5)
    )

    assert not wait.due(NOW)
    assert wait.due(NOW + timedelta(minutes=10))


def test_expire_is_logged_distinctly_from_resolve(tasks, log, conn):
    t = running_task(tasks)
    store = WaitStore(conn, log)
    wait = store.register(t.id, WaitKind.CI, {"branch": "x"}, NOW)
    store.expire(wait.id, "still pending after the deadline")

    kinds = [e.kind for e in log.for_task(t.id)]
    assert EventKind.WAIT_EXPIRED in kinds
    assert EventKind.WAIT_RESOLVED not in kinds
