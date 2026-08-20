"""Evidence text is read aloud and pasted into a live session.

Sloppy phrasing in a correction undermines the correction, so the wording is
worth a test of its own.
"""

from datetime import UTC, datetime, timedelta

from kestrel.events import EventKind
from kestrel.supervision import detect, minutes
from kestrel.tasks import TaskState

NOW = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)


def test_singular_and_plural_minutes():
    assert minutes(timedelta(minutes=1)) == "1 minute"
    assert minutes(timedelta(minutes=40)) == "40 minutes"
    assert minutes(timedelta(seconds=30)) == "0 minutes"


def test_repetition_evidence_reads_as_a_sentence(tasks, log):
    t = tasks.create("KES-32", "Sweep", ["tests pass"], "claude_code")
    tasks.transition(t.id, TaskState.BRIEFED)
    tasks.transition(t.id, TaskState.RUNNING)
    tasks.record_progress(t.id, "hash-a")
    for _ in range(40):
        log.append(
            EventKind.TOOL_CALL, "claude", {"tool": "Bash", "args": "gh run watch"}, task_id=t.id
        )

    evidence = detect(tasks.get(t.id), log.for_task(t.id), NOW).evidence

    assert "1 minutes" not in evidence
    assert evidence.startswith("you've run `Bash gh run watch` 40 times in 1 minute")
    assert evidence.endswith("continue with the task.")
