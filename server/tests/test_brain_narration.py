import pytest

from kestrel.brain.narration import NarrationKind, NarrationRequest, fallback_text, narrate
from kestrel.brain.runner import BrainError, BrainResult


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


def test_fallback_text_covers_every_kind_and_embeds_facts():
    for kind in NarrationKind:
        text = fallback_text(NarrationRequest(kind=kind, facts=["CI is green", "PR is up"]))
        assert "CI is green" in text
        assert "PR is up" in text


def test_fallback_text_with_no_facts_still_says_something():
    text = fallback_text(NarrationRequest(kind=NarrationKind.FINISHED, facts=[]))
    assert text.startswith("Done:")


async def test_narrate_uses_the_brain_when_it_succeeds(identity_dir):
    runner = FakeRunner(text="KES-31's done, PR's up.")
    text = await narrate(
        NarrationRequest(kind=NarrationKind.FINISHED, facts=["KES-31 merged"]),
        runner=runner,
        identity_dir=identity_dir,
    )
    assert text == "KES-31's done, PR's up."
    assert len(runner.calls) == 1


async def test_narrate_falls_back_to_the_template_on_a_brain_error(identity_dir):
    runner = FakeRunner(error=BrainError("brain is down"))
    failures = []
    text = await narrate(
        NarrationRequest(kind=NarrationKind.SOMETHING_WRONG, facts=["deploy failed twice"]),
        runner=runner,
        identity_dir=identity_dir,
        log_failure=failures.append,
    )
    assert text == fallback_text(
        NarrationRequest(kind=NarrationKind.SOMETHING_WRONG, facts=["deploy failed twice"])
    )
    assert len(failures) == 1


async def test_claude_question_reaches_the_text_verbatim_even_if_the_brain_drops_it(identity_dir):
    runner = FakeRunner(text="Nothing needs you.")
    text = await narrate(
        NarrationRequest(
            kind=NarrationKind.NEEDS_YOU,
            facts=["KES-34 stopped"],
            claude_question="Is the retry limit per-request or per-session?",
        ),
        runner=runner,
        identity_dir=identity_dir,
    )
    assert "Is the retry limit per-request or per-session?" in text


async def test_claude_question_is_not_duplicated_if_the_brain_already_included_it(identity_dir):
    question = "Is the retry limit per-request or per-session?"
    runner = FakeRunner(text=f"Claude's asking: {question}")
    text = await narrate(
        NarrationRequest(kind=NarrationKind.NEEDS_YOU, facts=[], claude_question=question),
        runner=runner,
        identity_dir=identity_dir,
    )
    assert text.count(question) == 1


async def test_narrate_falls_back_when_the_brain_returns_empty_text(identity_dir):
    runner = FakeRunner(text="")
    text = await narrate(
        NarrationRequest(kind=NarrationKind.FINISHED, facts=["done"]),
        runner=runner,
        identity_dir=identity_dir,
    )
    assert text == fallback_text(NarrationRequest(kind=NarrationKind.FINISHED, facts=["done"]))
