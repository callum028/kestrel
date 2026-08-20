import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from kestrel.events import EventKind
from kestrel.memory import MemoryStore, Source


@pytest.fixture
def memory(tmp_path, log):
    return MemoryStore(tmp_path / "memory", log)


def test_write_emits_a_visible_creation_event(memory, log):
    memory.write("Prefers integration tests for auth", "convention", Source.CORRECTION)
    written = log.of_kind(EventKind.MEMORY_WRITTEN)
    assert len(written) == 1
    assert "integration tests" in written[0].payload["fact"]


def test_every_write_is_a_git_commit(memory):
    memory.write("Works at a startup", "personal", Source.EXPLICIT)
    memory.write("Two projects run side by side", "personal", Source.EXPLICIT)
    out = subprocess.run(
        ["git", "log", "--oneline"], cwd=memory.repo, capture_output=True, text=True, check=False
    ).stdout
    assert len(out.strip().splitlines()) == 2


def test_round_trips_through_disk(memory):
    e = memory.write(
        "Exponential backoff on retries",
        "convention",
        Source.CORRECTION,
        scope="project:gymfront-edge",
        task_ref="KES-31",
    )
    loaded = memory.get(e.id)
    assert loaded.fact == "Exponential backoff on retries"
    assert loaded.source is Source.CORRECTION
    assert loaded.scope == "project:gymfront-edge"
    assert loaded.task_ref == "KES-31"


def test_contradiction_supersedes_rather_than_keeping_both(memory, log):
    old = memory.write("Fixed interval retries", "convention", Source.DECISION)
    new = memory.supersede(old.id, "Exponential backoff on retries")

    live = [e.fact for e in memory.all()]
    assert live == ["Exponential backoff on retries"]

    # audit trail survives, but is never read back into context
    archived = memory.get(old.id)
    assert archived.superseded_by == new.id
    assert archived.live is False
    assert len(memory.all(include_superseded=True)) == 2
    assert log.of_kind(EventKind.MEMORY_SUPERSEDED)


def test_project_scope_does_not_leak_between_repos(memory):
    memory.write("Global convention", "convention", Source.EXPLICIT)
    memory.write(
        "Edge-only convention", "convention", Source.EXPLICIT, scope="project:gymfront-edge"
    )
    memory.write(
        "Mobile-only convention", "convention", Source.EXPLICIT, scope="project:gymfront-mobile"
    )

    facts = {e.fact for e in memory.scoped("gymfront-edge")}
    assert facts == {"Global convention", "Edge-only convention"}


def test_core_is_the_always_injected_set_and_stays_out_of_recall(memory):
    memory.write("Callum works at a startup", "personal", Source.EXPLICIT, core=True)
    memory.write("The auth service uses rotating refresh tokens", "project", Source.DECISION)

    assert memory.core() == ["Callum works at a startup"]
    recalled = memory.recall("how does auth refresh work?")
    assert [e.fact for e in recalled] == ["The auth service uses rotating refresh tokens"]


def test_recall_is_automatic_and_matches_without_being_asked_to_look(memory):
    memory.write("Deploys to dev before testing, no localhost", "workflow", Source.EXPLICIT)
    memory.write("Prefers Playwright over Cypress", "convention", Source.CORRECTION)

    hits = [e.fact for e in memory.recall("did playwright pass?")]
    assert hits == ["Prefers Playwright over Cypress"]


def test_unused_entries_are_surfaced_not_expired(memory):
    old = memory.write("Something from months ago", "misc", Source.EXPLICIT)
    fresh = memory.write("Recent thing", "misc", Source.EXPLICIT)
    now = datetime.now(UTC)
    usage = {old.id: now - timedelta(days=120), fresh.id: now}

    stale = memory.unused_since(now - timedelta(days=90), usage)

    # Surfaced for a human decision. Nothing is deleted - auto-expiry deletes
    # true things, which is worse than carrying a stale fact you can see.
    assert [e.id for e in stale] == [old.id]
    assert memory.get(old.id).live is True


def test_there_is_no_inference_source():
    # Adding one would undo the design: nothing writes memory behind his back.
    assert {s.value for s in Source} == {"explicit", "correction", "decision", "proxy_answer"}
