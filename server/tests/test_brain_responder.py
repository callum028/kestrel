from datetime import UTC, datetime

import pytest

from kestrel.brain.responder import BrainResponder
from kestrel.brain.runner import BrainError, BrainResult
from kestrel.memory import MemoryStore
from kestrel.tasks import Task, TaskState


@pytest.fixture
def identity_dir(tmp_path):
    d = tmp_path / "identity"
    d.mkdir()
    (d / "identity.md").write_text("You are Kestrel.")
    (d / "examples.md").write_text("> Done.")
    return d


class FakeRunner:
    def __init__(self, text: str | None = None, error: Exception | None = None):
        self._text = text
        self._error = error
        self.calls: list[dict] = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return BrainResult(text=self._text or "", raw={}, model=kwargs["model"])


def make_responder(tmp_path, log, runner, **kwargs):
    memory = MemoryStore(tmp_path / "memory", log)
    return BrainResponder(
        runner=runner,
        identity_dir=kwargs.pop("identity_dir"),
        memory=memory,
        state_block=kwargs.pop("state_block", lambda: "presence: AT_DESK"),
        active_tasks=kwargs.pop("active_tasks", list),
        **kwargs,
    )


async def test_respond_returns_the_brains_text(tmp_path, log, identity_dir):
    runner = FakeRunner(text="KES-31's done.")
    responder = make_responder(tmp_path, log, runner, identity_dir=identity_dir)

    text = await responder.respond("what's running?", [])

    assert text == "KES-31's done."
    call = runner.calls[0]
    assert call["model"] == "haiku"
    assert call["allowed_tools"] == ["mcp__kestrel__*"]
    assert call["user_message"] == "what's running?"
    assert "You are Kestrel." in call["system_prompt"]


async def test_respond_uses_sonnet_for_a_depth_question(tmp_path, log, identity_dir):
    runner = FakeRunner(text="because of X")
    responder = make_responder(tmp_path, log, runner, identity_dir=identity_dir)

    await responder.respond("why did it choose that approach?", [])

    assert runner.calls[0]["model"] == "sonnet"


async def test_respond_propagates_a_brain_error_rather_than_swallowing_it(
    tmp_path, log, identity_dir
):
    runner = FakeRunner(error=BrainError("brain is down"))
    responder = make_responder(tmp_path, log, runner, identity_dir=identity_dir)

    with pytest.raises(BrainError):
        await responder.respond("hello", [])


async def test_respond_includes_active_tasks_in_the_prompt(tmp_path, log, identity_dir):
    runner = FakeRunner(text="ok")
    now = datetime.now(UTC)
    task = Task(
        id="t1",
        handle="KES-31",
        goal="fix auth",
        criteria=[],
        executor="claude_code",
        state=TaskState.RUNNING,
        created_at=now,
        updated_at=now,
    )
    responder = make_responder(
        tmp_path, log, runner, identity_dir=identity_dir, active_tasks=lambda: [task]
    )

    await responder.respond("what's running?", [])

    assert "KES-31: fix auth" in runner.calls[0]["system_prompt"]
