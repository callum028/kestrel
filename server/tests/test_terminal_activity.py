"""HumanActivityTracker - the signal `send` defers on.

Deliberately tiny: a dict of last-keystroke timestamps and a window check.
The interesting behaviour to prove is the boundary (just inside vs. just
outside the window) and that an untouched terminal reads as "no human here"
rather than erroring.
"""

from datetime import UTC, datetime, timedelta

from kestrel.terminal_activity import HumanActivityTracker


def test_untouched_terminal_is_not_active():
    tracker = HumanActivityTracker()
    assert tracker.active("term-1") is False


def test_a_recent_keystroke_marks_the_terminal_active():
    tracker = HumanActivityTracker(window=timedelta(minutes=2))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tracker.mark("term-1", at=now)
    assert tracker.active("term-1", now=now + timedelta(seconds=30)) is True


def test_activity_expires_after_the_window():
    tracker = HumanActivityTracker(window=timedelta(minutes=2))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tracker.mark("term-1", at=now)
    assert tracker.active("term-1", now=now + timedelta(minutes=3)) is False


def test_terminals_are_tracked_independently():
    tracker = HumanActivityTracker(window=timedelta(minutes=2))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tracker.mark("term-1", at=now)
    assert tracker.active("term-2", now=now) is False
