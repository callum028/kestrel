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
    extract_blocked_claim,
    introduced_blocker_markers,
    next_action,
    unrequested_spec_changes,
    validate_blocker,
)
from kestrel.tasks import Task, TaskState

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


# --- new detectors: idle-with-no-wait, startup, process death, pending waits -


def test_idle_with_no_hook_activity_and_no_wait_is_a_stall(tasks, log):
    t = running_task(tasks)
    later = t.created_at + timedelta(minutes=25)
    events = poll_events(log, t.id, 1)  # one event, then silence
    # Force the single event's timestamp far enough in the past by using a
    # `now` far ahead of it - poll_events uses real wall-clock timestamps, so
    # the "later" here only needs to be far enough past *now*, which it is.
    verdict = detect(t, events, later)
    assert verdict.stalled
    assert verdict.reason is StallReason.IDLE_NO_WAIT
    assert "kestrel-wait" in verdict.evidence


def test_a_pending_wait_suppresses_idle_and_no_progress():
    t_created = NOW

    t = Task(
        id="t1",
        handle="KES-99",
        goal="goal",
        criteria=[],
        executor="claude_code",
        state=TaskState.RUNNING,
        created_at=t_created,
        last_progress_at=t_created,
    )
    later = t_created + timedelta(minutes=40)
    verdict = detect(t, [], later, has_pending_wait=True)
    assert not verdict.stalled


def test_a_pending_wait_does_not_suppress_the_wall_clock_budget():

    t = Task(
        id="t1",
        handle="KES-99",
        goal="goal",
        criteria=[],
        executor="claude_code",
        state=TaskState.RUNNING,
        created_at=NOW,
    )
    verdict = detect(
        t,
        [],
        NOW + timedelta(hours=9),
        Budget(wall_clock=timedelta(hours=2)),
        has_pending_wait=True,
    )
    assert verdict.reason is StallReason.WALL_CLOCK


def test_startup_stuck_when_session_never_gets_past_the_prompt(tasks, log):
    t = running_task(tasks)
    log.append(EventKind.SESSION_STARTED, "kestrel", {}, task_id=t.id)
    events = log.for_task(t.id)
    started_at = next(e.ts for e in events if e.kind is EventKind.SESSION_STARTED)

    verdict = detect(t, events, started_at + timedelta(minutes=10))
    assert verdict.stalled
    assert verdict.reason is StallReason.STARTUP_STUCK


def test_startup_is_fine_once_session_bound_fires(tasks, log):
    t = running_task(tasks)
    log.append(EventKind.SESSION_STARTED, "kestrel", {}, task_id=t.id)
    log.append(EventKind.SESSION_BOUND, "agent", {}, task_id=t.id)
    events = log.for_task(t.id)
    started_at = next(e.ts for e in events if e.kind is EventKind.SESSION_STARTED)

    verdict = detect(t, events, started_at + timedelta(minutes=10))
    assert verdict.reason is not StallReason.STARTUP_STUCK


def test_startup_is_fine_once_a_prompt_is_submitted(tasks, log):
    t = running_task(tasks)
    log.append(EventKind.SESSION_STARTED, "kestrel", {}, task_id=t.id)
    log.append(EventKind.PROMPT_SUBMITTED, "claude", {"prompt": "go"}, task_id=t.id)
    events = log.for_task(t.id)
    started_at = next(e.ts for e in events if e.kind is EventKind.SESSION_STARTED)

    verdict = detect(t, events, started_at + timedelta(minutes=10))
    assert verdict.reason is not StallReason.STARTUP_STUCK


def test_extract_blocked_claim_finds_what_a_claim_says_its_stuck_on():
    assert extract_blocked_claim("I'm blocked on LEGACY_SYNC being enabled") == "LEGACY_SYNC"
    assert extract_blocked_claim("Waiting on `FEATURE_X` to land") == "FEATURE_X"
    assert extract_blocked_claim("Done, all tests pass.") is None
    assert extract_blocked_claim(None) is None
