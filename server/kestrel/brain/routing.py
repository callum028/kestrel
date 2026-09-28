"""Model routing for the brain - Haiku by default, Sonnet for
judgement-heavy turns.

Deliberately not the `Router`/`Provider`/`CallClass` machinery in
`models.py` - that interface routes *providers* (Groq, Gemini, Claude Code
headless) for a design that is not this step's hard constraint. Here there is
exactly one provider, ever: the headless CLI. The only choice left is which
model flag to pass it, so it gets the smallest interface that could do that:
one function.

Kept a plain function rather than a class so a future Jev-based router (per
the step brief) is a drop-in replacement with the same signature, not a
subclass to maintain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..conversation import ConversationMessage
from .runner import Model

# Phrases that ask for depth rather than a quick answer. Deliberately small
# and literal - see the module docstring on why this is not a classifier.
_DEPTH_PHRASES = (
    "why",
    "explain",
    "walk me through",
    "in depth",
    "in detail",
    "think it through",
    "what's the tradeoff",
    "what are the tradeoffs",
    "reasoning",
    "compare",
    "pros and cons",
)


@dataclass(frozen=True)
class ModelNames:
    """The `--model` alias/name to pass the CLI for each tier - configurable
    via `KESTREL_BRAIN_HAIKU_MODEL`/`KESTREL_BRAIN_SONNET_MODEL` (see
    `config.Config`), since the CLI's own built-in aliases (`haiku`/`sonnet`)
    are a moving target and a deployment may want to pin an exact model
    string instead. Defaults match those aliases, so an unconfigured
    deployment behaves exactly as it did when `decide_model` returned them
    as literals."""

    haiku: str = "haiku"
    sonnet: str = "sonnet"


DEFAULT_MODEL_NAMES = ModelNames()


@dataclass(frozen=True)
class Turn:
    """Everything `decide_model` needs to know about one turn. A dataclass
    rather than passing `text`/`history`/`summarising_task` positionally, so
    a future router can add a field without every call site changing."""

    text: str
    history: list[ConversationMessage] = field(default_factory=list)
    # Set when this turn is Kestrel summarising a just-finished task's full
    # report for Callum, rather than answering something he said - the one
    # other case the step brief calls out for Sonnet explicitly.
    summarising_task_report: bool = False


def decide_model(turn: Turn, models: ModelNames = DEFAULT_MODEL_NAMES) -> Model:
    """Haiku unless the turn asks for depth or is a finished-task summary.

    Replaceable: nothing else in `brain/` depends on this being a keyword
    match rather than a classifier - swap the body out and every caller
    (responder.py, narration.py) keeps working. `models` is the only place
    the actual `--model` strings come from - see `ModelNames`.
    """
    if turn.summarising_task_report:
        return models.sonnet
    text = turn.text.lower()
    if any(re.search(rf"\b{re.escape(phrase)}\b", text) for phrase in _DEPTH_PHRASES):
        return models.sonnet
    if text.count("?") >= 2:
        # More than one question in a turn tends to be "walk me through this
        # and also X" rather than a single lookup.
        return models.sonnet
    return models.haiku
