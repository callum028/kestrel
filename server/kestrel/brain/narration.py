"""Narration - wording a delivery in Kestrel's voice, via the brain.

"Mediates, never filters" (docs/design.md §1): the model may choose how a
fact is phrased, never what the fact says. So the caller's `facts` and any
`claude_question` are the things that must survive into the final text
unchanged - `narrate()` asks the brain to reword them, then checks the
question actually made it through verbatim and appends it itself if the
model dropped or paraphrased it. A brain call that fails - timeout, bad exit,
unparseable output - falls back to a fixed deterministic template rather than
losing the delivery, and the failure is logged (`EventKind.BRAIN_CALL_FAILED`)
so a dead brain is never a silent one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .context_assembly import load_identity
from .routing import Turn, decide_model
from .runner import BrainError, BrainRunner


class NarrationKind(StrEnum):
    NEEDS_YOU = "needs_you"
    FINISHED = "finished"
    SOMETHING_WRONG = "something_wrong"


@dataclass(frozen=True)
class NarrationRequest:
    kind: NarrationKind
    facts: list[str] = field(default_factory=list)
    # Claude's own words, if this delivery is carrying a question from a
    # supervised session - reaches Callum unchanged, per §1.
    claude_question: str | None = None


_TEMPLATE_LEAD = {
    NarrationKind.NEEDS_YOU: "Needs you:",
    NarrationKind.FINISHED: "Done:",
    NarrationKind.SOMETHING_WRONG: "Something's wrong:",
}


def fallback_text(request: NarrationRequest) -> str:
    """The deterministic template - no model, no failure mode of its own.
    Used both when the brain call fails and as the thing tests pin the
    contract against."""
    body = ". ".join(f for f in request.facts if f) or "(no detail)"
    text = f"{_TEMPLATE_LEAD[request.kind]} {body}"
    if request.claude_question:
        text = f"{text}\n\nClaude's asking: {request.claude_question}"
    return text


def _prompt(request: NarrationRequest) -> str:
    facts_block = "\n".join(f"- {f}" for f in request.facts) or "- (no facts given)"
    return (
        f"Write one short Kestrel-voiced message for Callum about a '{request.kind}' "
        "delivery. Reword only - do not invent a fact, do not drop one, and do not add "
        "a question of your own.\n\nFacts:\n"
        f"{facts_block}"
    )


async def narrate(
    request: NarrationRequest,
    *,
    runner: BrainRunner,
    identity_dir: Path,
    log_failure: object | None = None,
) -> str:
    """`log_failure`, when given, is called with the exception on a brain
    failure - kept a plain optional callable rather than importing
    `EventLog` here, so this module has no dependency on the event log's
    shape (narration.py is usable standalone, e.g. from a script)."""
    try:
        identity = load_identity(identity_dir)
        model = decide_model(Turn(text=" ".join(request.facts)))
        result = await runner.run(
            system_prompt=f"{identity.text}\n\n## Voice\n{identity.examples}",
            user_message=_prompt(request),
            model=model,
        )
        text = result.text.strip() or fallback_text(request)
    except BrainError as exc:
        if log_failure is not None:
            log_failure(exc)
        text = fallback_text(request)

    if request.claude_question and request.claude_question not in text:
        text = f"{text}\n\nClaude's asking: {request.claude_question}"
    return text
