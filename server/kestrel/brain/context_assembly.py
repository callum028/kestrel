"""Wires Runtime's pieces into `context.assemble()` for one brain turn.

Kept out of `context.py` on purpose: that module stays a pure function with
no knowledge of `Runtime`, tasks, or memory storage - everything here is
"what to gather", everything there is "how to lay it out" (docs/design.md
§4.5). `context.py`'s `assemble()` signature is unchanged.

One extra step this module owns that a chat-completions API would not need:
`claude -p` takes exactly one system prompt and one query string, not a list
of prior turns - there is no `--resume`, by design (see `runner.py`). So the
conversation history that `assemble()` would otherwise hand back as
`messages` for an API call is instead folded into the system prompt text
here (`render_for_headless`), and only the newest message is passed as the
query.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..context import PromptBundle, assemble
from ..models import Message

if TYPE_CHECKING:  # pragma: no cover - avoids a runtime<->brain import cycle
    from ..conversation import ConversationMessage
    from ..memory import MemoryStore
    from ..tasks import Task

# How many prior turns ride along in the system prompt. Bounded, per "nothing
# enters context by accumulation" - this is a fixed window, not a growing
# transcript.
DEFAULT_HISTORY_TURNS = 20

_ROLE_MAP = {"user": "user", "kestrel": "assistant", "system": "system"}


@dataclass(frozen=True)
class Identity:
    text: str
    examples: str


def load_identity(identity_dir: Path) -> Identity:
    return Identity(
        text=(identity_dir / "identity.md").read_text(),
        examples=(identity_dir / "examples.md").read_text(),
    )


def task_index(tasks: list[Task]) -> list[str]:
    """Handles are the exact arguments the tools take (`start_task`,
    `task_detail`, ...), so this doubles as the pointer index §4.5 asks for -
    retrieval becomes one hop instead of a search."""
    return [f"{t.handle}: {t.goal} ({t.state}, {t.nudges} nudges)" for t in tasks]


def build_prompt(
    *,
    identity_dir: Path,
    memory: MemoryStore,
    attention_block: str,
    active_tasks: list[Task],
    history: list[ConversationMessage],
    message: str,
    project: str | None = None,
    history_turns: int = DEFAULT_HISTORY_TURNS,
) -> PromptBundle:
    identity = load_identity(identity_dir)
    recent = history[-history_turns:] if history_turns else history
    conversation = [
        Message(role=_ROLE_MAP.get(m.role, "assistant"), content=m.text) for m in recent
    ]
    conversation.append(Message(role="user", content=message))
    recalled = [entry.fact for entry in memory.recall(message, project)]
    return assemble(
        identity=identity.text,
        examples=identity.examples,
        memory_core=memory.core(project),
        attention_block=attention_block,
        task_index=task_index(active_tasks),
        conversation=conversation,
        retrieved=recalled,
    )


def render_for_headless(bundle: PromptBundle) -> tuple[str, str]:
    """`(system_prompt, query)` for `BrainRunner.run` - everything but the
    newest message folded into the system text, per the module docstring."""
    if not bundle.messages:
        return bundle.system, ""
    *history, latest = bundle.messages
    parts = [bundle.system]
    if history:
        transcript = "\n".join(f"{m.role}: {m.content}" for m in history)
        parts.append(f"## Conversation so far\n{transcript}")
    return "\n\n".join(parts), latest.content
