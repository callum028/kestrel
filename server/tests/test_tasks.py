import pytest

from kestrel.events import EventKind
from kestrel.tasks import IllegalTransition, TaskState


def make(tasks, handle="KES-1"):
    return tasks.create(
        handle=handle,
        goal="Handle token refresh failure",
        criteria=["npm test passes", "CI green"],
        executor="claude_code",
    )


def test_create_writes_an_event(tasks, log):
    t = make(tasks)
    kinds = [e.kind for e in log.for_task(t.id)]
    assert EventKind.TASK_CREATED in kinds
    assert t.state is TaskState.CREATED


def test_illegal_transition_is_refused(tasks):
    t = make(tasks)
    with pytest.raises(IllegalTransition):
        tasks.transition(t.id, TaskState.DONE)


def test_failed_validation_reopens_rather_than_reverting(tasks):
    t = make(tasks)
    tasks.transition(t.id, TaskState.BRIEFED)
    tasks.transition(t.id, TaskState.RUNNING)
    tasks.transition(t.id, TaskState.AWAITING_DEV)
    tasks.transition(t.id, TaskState.VALIDATING)
    reopened = tasks.transition(t.id, TaskState.RUNNING, reason="playwright: 2 failures")
    assert reopened.state is TaskState.RUNNING


def test_activity_without_diff_change_is_not_progress(tasks):
    t = make(tasks)
    assert tasks.record_progress(t.id, "hash-a") is True
    assert tasks.record_progress(t.id, "hash-a") is False  # the polling-loop case
    assert tasks.record_progress(t.id, "hash-b") is True


def test_progress_resets_the_nudge_budget(tasks):
    t = make(tasks)
    tasks.record_nudge(t.id, "gh run watch x40, no diff change")
    assert tasks.record_nudge(t.id, "still polling") == 2
    tasks.record_progress(t.id, "hash-a")
    assert tasks.get(t.id).nudges == 0
