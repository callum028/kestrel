"""`Runtime.check_completion_claim` - "done" is a claim, not a fact.

Wired at the `/hooks/claude` Stop handler (test_api.py covers that plumbing);
this file drives it directly against a fake executor so each check can be
isolated from git and the session host.
"""

import pytest

from kestrel.config import Config
from kestrel.events import EventKind
from kestrel.executors import ExecutorKind
from kestrel.outcomes import Ok
from kestrel.runtime import Runtime
from kestrel.supervision import Diff


class FakeExecutor:
    kind = ExecutorKind.CLAUDE_CODE

    def __init__(self, diff: Diff, symbols: set[str] | None = None) -> None:
        self._diff = diff
        self._symbols = symbols or set()
        self.sent: list[str] = []

    async def start(self, task, brief):
        pass

    async def send(self, task, message):
        self.sent.append(message)
        return Ok()

    async def stop(self, task, reason):
        pass

    async def diff(self, task):
        return self._diff

    async def symbol_exists(self, task, symbol):
        return symbol in self._symbols


@pytest.fixture
def config(tmp_path):
    return Config(
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        memory_repo=tmp_path / "memory",
        identity_dir=tmp_path / "identity",
    )


def _runtime(config, executor: FakeExecutor) -> Runtime:
    return Runtime.build(config, executors={str(ExecutorKind.CLAUDE_CODE): executor})


async def test_an_empty_diff_rejects_the_claim(config):
    executor = FakeExecutor(Diff(added=[], removed=[], files=[]))
    rt = _runtime(config, executor)
    task = rt.tasks.create("KES-1", "Fix it", ["tests pass"], str(ExecutorKind.CLAUDE_CODE))

    outcome = await rt.check_completion_claim(task.id, "Done, all tests pass.")

    assert outcome.status == "refused"
    assert "empty" in outcome.reason
    assert executor.sent  # nudged with the evidence
    rejected = [e for e in rt.log.for_task(task.id) if e.kind is EventKind.CLAIM_REJECTED]
    assert len(rejected) == 1


async def test_an_introduced_todo_marker_rejects_the_claim(config):
    diff = Diff(
        added=["// TODO: re-enable once LEGACY_SYNC is on", "const x = 1;"],
        removed=[],
        files=["src/a.ts"],
    )
    executor = FakeExecutor(diff)
    rt = _runtime(config, executor)
    task = rt.tasks.create("KES-2", "Fix it", ["tests pass"], str(ExecutorKind.CLAUDE_CODE))

    outcome = await rt.check_completion_claim(task.id, "Done.")

    assert outcome.status == "refused"
    assert "LEGACY_SYNC" in outcome.reason


async def test_unrequested_spec_edits_reject_the_claim(config):
    diff = Diff(added=[], removed=[], files=["src/auth/token.ts", "src/auth/token.spec.ts"])
    executor = FakeExecutor(diff)
    rt = _runtime(config, executor)
    task = rt.tasks.create(
        "KES-3", "Fix it", ["refresh failure bounces to login"], str(ExecutorKind.CLAUDE_CODE)
    )

    outcome = await rt.check_completion_claim(task.id, "Done.")

    assert outcome.status == "refused"
    assert "token.spec.ts" in outcome.reason


async def test_a_blocker_claim_naming_something_the_diff_deletes_is_rejected(config):
    diff = Diff(
        added=["fixed unrelated thing"],
        removed=["  if (LEGACY_SYNC) {", "  const LEGACY_SYNC = false;"],
        files=["src/a.ts"],
    )
    executor = FakeExecutor(diff, symbols={"LEGACY_SYNC"})
    rt = _runtime(config, executor)
    task = rt.tasks.create("KES-4", "Fix it", ["tests pass"], str(ExecutorKind.CLAUDE_CODE))

    outcome = await rt.check_completion_claim(
        task.id, "I'm blocked on LEGACY_SYNC being re-enabled."
    )

    assert outcome.status == "refused"
    assert "deletes it" in outcome.reason


async def test_a_blocker_claim_naming_something_that_does_not_exist_is_rejected(config):
    diff = Diff(added=["fixed unrelated thing"], removed=[], files=["src/a.ts"])
    executor = FakeExecutor(diff, symbols=set())
    rt = _runtime(config, executor)
    task = rt.tasks.create("KES-5", "Fix it", ["tests pass"], str(ExecutorKind.CLAUDE_CODE))

    outcome = await rt.check_completion_claim(task.id, "Blocked on FEATURE_X landing first.")

    assert outcome.status == "refused"
    assert "does not exist" in outcome.reason


async def test_a_clean_completion_claim_is_accepted(config):
    diff = Diff(added=["+ real fix"], removed=["- old bug"], files=["src/a.ts"])
    executor = FakeExecutor(diff)
    rt = _runtime(config, executor)
    task = rt.tasks.create("KES-6", "Fix it", ["tests pass"], str(ExecutorKind.CLAUDE_CODE))

    outcome = await rt.check_completion_claim(task.id, "Done, all tests pass.")

    assert outcome.status == "ok"
    assert executor.sent == []
    accepted = [e for e in rt.log.for_task(task.id) if e.kind is EventKind.CLAIM_ACCEPTED]
    assert len(accepted) == 1


async def test_no_worktree_capable_executor_is_a_pass_through(config):
    """An executor without `diff` (kestrel/human/phone) has nothing for claim
    validation to check against - it must not raise or reject."""
    rt = Runtime.build(config)  # no executors registered at all
    task = rt.tasks.create("KES-7", "Fix it", ["tests pass"], "some_other_executor")

    outcome = await rt.check_completion_claim(task.id, "Done.")
    assert outcome.status == "ok"
