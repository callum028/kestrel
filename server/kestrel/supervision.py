"""Supervision - the reason the system exists.

Every detector here is deterministic. None of it involves a model, which is what
stops it inheriting the unreliability it is supervising away.

The failure this was written for: a task list left running overnight, the first
item done, then nine hours in a polling loop. All the machinery below would have
caught that inside fifteen minutes, and the fix was one sentence.

Claude supplies the capability; Kestrel supplies the persistence.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .events import Event, EventKind
from .outcomes import Ambiguous, Ok, Outcome, Refused
from .tasks import Task

NO_PROGRESS_AFTER = timedelta(minutes=15)
REPETITION_THRESHOLD = 6
MAX_NUDGES = 3

# No hook activity of any kind (not even a tool call that leaves the worktree
# untouched) for this long, with no wait registered, is almost always a
# broken watcher rather than genuine thinking time - the session has gone
# quiet on a shell polling loop `kestrel-wait` should have replaced. Shorter
# than NO_PROGRESS_AFTER deliberately: silence is a stronger, earlier signal
# than "busy but the diff isn't moving".
IDLE_NO_WAIT_AFTER = timedelta(minutes=10)

# A `claude` process that never fires SessionStart's bind or a first
# UserPromptSubmit within this long of the terminal being created is very
# likely blocked on a workspace-trust or bypass-permissions confirmation it
# cannot get past unattended.
STARTUP_TIMEOUT = timedelta(minutes=5)

# Markers a session introduces that assert a dependency. A comment claiming to
# wait on something is a smell worth seeing every time.
BLOCKER_MARKERS = ("todo", "waiting on", "waiting for", "blocked on", "disabled until")

# What a completion claim's own text is checked against for a stated blocker -
# "blocked on LEGACY_SYNC", "waiting on the API team", 'waiting for `FLAG`'.
_BLOCKED_CLAIM = re.compile(
    r"(?:blocked on|waiting on|waiting for)\s+[`\"']?([\w./-]+)[`\"']?", re.IGNORECASE
)


def extract_blocked_claim(text: str | None) -> str | None:
    """The thing a completion claim says it is stuck on, if it says so at all.
    Used to feed `validate_blocker` from a Stop hook's `last_assistant_message`
    - "blocked" is a claim exactly like "done" is, so it gets named and checked
    rather than taken on trust."""
    if not text:
        return None
    match = _BLOCKED_CLAIM.search(text)
    return match.group(1) if match else None


class StallReason(StrEnum):
    NONE = "none"
    NO_PROGRESS = "no_progress"
    REPETITION = "repetition"
    WALL_CLOCK = "wall_clock"
    TURNS = "turns"
    IDLE_NO_WAIT = "idle_no_wait"
    PROCESS_DIED = "process_died"
    STARTUP_STUCK = "startup_stuck"


class Action(StrEnum):
    """The intervention ladder. Killing is the last rung, not the first."""

    NUDGE = "nudge"
    NUDGE_HARDER = "nudge_harder"
    RESTART = "restart"
    PARK = "park"


@dataclass(frozen=True)
class Budget:
    wall_clock: timedelta = timedelta(hours=2)
    turns: int = 120


@dataclass(frozen=True)
class StallVerdict:
    stalled: bool
    reason: StallReason
    evidence: str = ""

    def __bool__(self) -> bool:
        return self.stalled


def minutes(delta: timedelta) -> str:
    """Evidence gets read aloud and pasted into a session. "1 minutes" reads as
    carelessness, and carelessness in the evidence undermines the correction."""
    count = int(delta.total_seconds() // 60)
    return f"{count} minute" if count == 1 else f"{count} minutes"


def _repeated_call(events: list[Event]) -> tuple[str, int] | None:
    calls = [
        f"{e.payload.get('tool', '?')} {e.payload.get('args', '')}".strip()
        for e in events
        if e.kind is EventKind.TOOL_CALL
    ]
    if not calls:
        return None
    call, count = Counter(calls).most_common(1)[0]
    return (call, count) if count >= REPETITION_THRESHOLD else None


def _session_started_at(events: list[Event]) -> datetime | None:
    for e in events:
        if e.kind is EventKind.SESSION_STARTED:
            return e.ts
    return None


def _startup_completed(events: list[Event]) -> bool:
    return any(e.kind in (EventKind.SESSION_BOUND, EventKind.PROMPT_SUBMITTED) for e in events)


def detect(
    task: Task,
    events: list[Event],
    now: datetime,
    budget: Budget | None = None,
    has_pending_wait: bool = False,
) -> StallVerdict:
    """Activity is not progress. A polling loop produces plenty of activity.

    Checked cheapest-and-most-certain first, so the evidence returned is the most
    specific true statement available - which matters, because that evidence is
    used verbatim as the nudge.

    `has_pending_wait` is set by the caller when the task has a live
    `waits.Wait` registered (see `waits.py`). A session that correctly
    registered a wait and ended its turn is not stalled by definition - it is
    doing exactly what it should - so the no-progress and idle checks below
    are suppressed while one is outstanding. The wall-clock/turn budgets and
    the repetition check still apply: a wait does not excuse a session that is
    *also* burning turns on something else.
    """
    budget = budget or Budget()

    elapsed = now - task.created_at
    if elapsed > budget.wall_clock:
        return StallVerdict(
            True,
            StallReason.WALL_CLOCK,
            f"this task has been running {minutes(elapsed)}, "
            f"past its {minutes(budget.wall_clock)} budget",
        )

    turns = sum(1 for e in events if e.kind is EventKind.TOOL_CALL)
    if turns > budget.turns:
        return StallVerdict(
            True, StallReason.TURNS, f"{turns} tool calls on one task, past the {budget.turns} cap"
        )

    started_at = _session_started_at(events)
    if (
        started_at is not None
        and not _startup_completed(events)
        and now - started_at > STARTUP_TIMEOUT
    ):
        return StallVerdict(
            True,
            StallReason.STARTUP_STUCK,
            f"the session for {task.handle} started {minutes(now - started_at)} ago but never "
            f"got past startup - probably a workspace-trust or permissions prompt waiting on "
            f"an answer nobody can give it unattended.",
        )

    repeated = _repeated_call(events)
    if repeated is not None:
        call, count = repeated
        return StallVerdict(
            True,
            StallReason.REPETITION,
            f"you've run `{call}` {count} times in {_window(events, now)} with no change - "
            f"it isn't going to report back. Stop waiting and continue with the task.",
        )

    if has_pending_wait:
        return StallVerdict(False, StallReason.NONE)

    quiet = now - events[-1].ts if events else timedelta(0)
    if events and quiet > IDLE_NO_WAIT_AFTER:
        return StallVerdict(
            True,
            StallReason.IDLE_NO_WAIT,
            f"you've been idle {minutes(quiet)} and nothing is pending - what are you waiting "
            f"on? If it's real, register it with `kestrel-wait` and stop. Otherwise, check "
            f"directly and continue.",
        )

    last = task.last_progress_at or task.created_at
    idle = now - last
    if idle > NO_PROGRESS_AFTER:
        return StallVerdict(
            True,
            StallReason.NO_PROGRESS,
            f"nothing in the worktree has changed for {minutes(idle)}. "
            f"If you're waiting on something, say what. Otherwise continue.",
        )

    return StallVerdict(False, StallReason.NONE)


def _window(events: list[Event], now: datetime) -> str:
    calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    if not calls:
        return "no time at all"
    return minutes(max(now - calls[0].ts, timedelta(minutes=1)))


def next_action(nudges_sent: int) -> Action:
    """Budget the nudges, or the nudge loop becomes its own overnight failure."""
    if nudges_sent == 0:
        return Action.NUDGE
    if nudges_sent < MAX_NUDGES:
        return Action.NUDGE_HARDER
    if nudges_sent == MAX_NUDGES:
        return Action.RESTART
    return Action.PARK


# --- claim validation -------------------------------------------------------
# "Blocked" is a claim exactly like "done" is a claim. Both get checked.


@dataclass(frozen=True)
class Diff:
    added: list[str]
    removed: list[str]
    files: list[str]


def validate_blocker(thing: str | None, diff: Diff, repo_symbols: set[str]) -> Outcome:
    """A reported blocker must name what it is waiting on, and that thing has to
    survive contact with the diff.

    The case this exists for: a session claiming to wait on a flag being enabled
    while deleting the code for that flag in the same change.
    """
    if not thing:
        return Refused(reason="a blocker has to name what it is waiting on")

    if any(thing in line for line in diff.removed):
        return Refused(
            reason=f"claims to be waiting on {thing}, but this diff deletes it. "
            f"That dependency is gone - continue without it."
        )

    if thing not in repo_symbols:
        return Refused(
            reason=f"{thing} does not exist in the repo, so it cannot be blocking anything"
        )

    return Ok(value=thing)


def unrequested_spec_changes(diff: Diff, criteria: list[str]) -> Outcome:
    """The most common way a suite goes green without the code being fixed."""
    specs = [f for f in diff.files if ".spec." in f or ".test." in f or "/tests/" in f]
    if not specs:
        return Ok()
    asked = " ".join(criteria).lower()
    unrequested = [s for s in specs if "test" not in asked and "spec" not in asked]
    if not unrequested:
        return Ok(value=specs)
    return Ambiguous(
        searched_for="test changes the criteria did not ask for", candidates=unrequested
    )


def introduced_blocker_markers(diff: Diff) -> list[str]:
    return [line.strip() for line in diff.added if any(m in line.lower() for m in BLOCKER_MARKERS)]
