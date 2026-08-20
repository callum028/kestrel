"""The overnight failure, encoded.

A task list left running, the first item done, then nine hours polling. Every
test here is a piece of machinery that would have caught it.
"""

from datetime import UTC, datetime, timedelta

from kestrel.events import EventKind
from kestrel.supervision import (
    MAX_NUDGES,
    Action,
    Budget,
    Diff,
    StallReason,
    detect,
    introduced_blocker_markers,
    next_action,
    unrequested_spec_changes,
    validate_blocker,
)
from kestrel.tasks import TaskState

NOW = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)


def running_task(tasks):
    t = tasks.create("KES-32", "Backlog sweep", ["npm test passes"], "claude_code")
    tasks.transition(t.id, TaskState.BRIEFED)
    tasks.transition(t.id, TaskState.RUNNING)
    tasks.record_progress(t.id, "hash-a")
    return tasks.get(t.id)


def poll_events(log, task_id, n, tool="gh run watch"):
    for _ in range(n):
        log.append(EventKind.TOOL_CALL, "claude", {"tool": tool, "args": ""}, task_id=task_id)
    return log.for_task(task_id)


def test_the_polling_loop_is_caught(tasks, log):
    t = running_task(tasks)
    events = poll_events(log, t.id, 40)

    verdict = detect(t, events, NOW)

    assert verdict.stalled
    assert verdict.reason is StallReason.REPETITION
    # The detector's output is the nudge. Generic "please continue" does not work.
    assert "gh run watch" in verdict.evidence
    assert "40 times" in verdict.evidence


def test_varied_tool_calls_are_not_a_stall(tasks, log):
    t = running_task(tasks)
    for i in range(40):
        log.append(
            EventKind.TOOL_CALL, "claude", {"tool": "edit", "args": f"file{i}.ts"}, task_id=t.id
        )
    t = tasks.get(t.id)
    assert not detect(t, log.for_task(t.id), NOW).stalled


def test_silence_with_no_diff_change_is_a_stall(tasks, log):
    t = running_task(tasks)
    later = (t.last_progress_at or t.created_at) + timedelta(minutes=40)
    verdict = detect(t, [], later)
    assert verdict.reason is StallReason.NO_PROGRESS


def test_wall_clock_budget_catches_what_detectors_miss(tasks, log):
    t = running_task(tasks)
    verdict = detect(
        t, [], t.created_at + timedelta(hours=9), Budget(wall_clock=timedelta(hours=2))
    )
    assert verdict.reason is StallReason.WALL_CLOCK


def test_ladder_nudges_before_it_kills():
    assert next_action(0) is Action.NUDGE
    assert next_action(1) is Action.NUDGE_HARDER
    assert next_action(MAX_NUDGES) is Action.RESTART
    assert next_action(MAX_NUDGES + 1) is Action.PARK


def test_a_blocker_must_name_itself():
    result = validate_blocker(None, Diff([], [], []), {"LEGACY_SYNC"})
    assert result.status == "refused"


def test_waiting_on_something_the_same_diff_deletes():
    """The second half of the overnight failure, verbatim."""
    diff = Diff(
        added=[], removed=["  if (LEGACY_SYNC) {", "  const LEGACY_SYNC = false;"], files=[]
    )
    result = validate_blocker("LEGACY_SYNC", diff, {"LEGACY_SYNC"})
    assert result.status == "refused"
    assert "deletes it" in result.reason


def test_blocker_that_does_not_exist_at_all():
    result = validate_blocker("FEATURE_X", Diff([], [], []), {"LEGACY_SYNC"})
    assert result.status == "refused"
    assert "does not exist" in result.reason


def test_a_real_blocker_is_accepted():
    result = validate_blocker("LEGACY_SYNC", Diff([], [], ["src/a.ts"]), {"LEGACY_SYNC"})
    assert result.status == "ok"


def test_tests_edited_without_being_asked_are_flagged():
    diff = Diff([], [], ["src/auth/token.ts", "src/auth/token.spec.ts"])
    result = unrequested_spec_changes(diff, ["refresh failure bounces to login"])
    assert result.status == "ambiguous"
    assert "src/auth/token.spec.ts" in result.candidates


def test_tests_edited_when_the_criteria_asked_for_it_are_fine():
    diff = Diff([], [], ["src/auth/token.spec.ts"])
    result = unrequested_spec_changes(diff, ["add a test for refresh failure"])
    assert result.status == "ok"


def test_markers_asserting_a_dependency_are_surfaced():
    diff = Diff(
        added=["// TODO: re-enable once LEGACY_SYNC is turned on", "const x = 1;"],
        removed=[],
        files=[],
    )
    assert introduced_blocker_markers(diff) == ["// TODO: re-enable once LEGACY_SYNC is turned on"]
