"""Context assembly - a pure function.

Nothing enters context by accumulation. Every token is static, computed, or
retrieved; if something grows on its own, it is a bug. That rule is what fixes
the problem v1 never solved (what to store, and for how long).

assemble() is pure and dumpable so two turns can be diffed. Context problems are
invisible when the prompt is built implicitly across a codebase, and trivial when
it is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .models import Message

# Hard cap on the always-injected core. Past this, memory is retrieved rather
# than injected - the cap is what forces curation to actually happen.
MEMORY_CORE_MAX = 40


@dataclass(frozen=True)
class PromptBundle:
    system: str
    messages: list[Message]
    layers: dict[str, int] = field(default_factory=dict)

    def dump(self) -> str:
        return json.dumps(
            {
                "layers": self.layers,
                "system": self.system,
                "messages": [{"role": m.role, "content": m.content} for m in self.messages],
            },
            indent=2,
        )


def _section(title: str, body: str) -> str:
    return f"## {title}\n{body.strip()}\n"


def assemble(
    *,
    identity: str,
    examples: str,
    memory_core: list[str],
    attention_block: str,
    task_index: list[str],
    conversation: list[Message],
    retrieved: list[str] | None = None,
    summary: str | None = None,
) -> PromptBundle:
    """Build the prompt for one turn.

    task_index carries handles that are the exact arguments the tools take, so
    retrieval is one hop rather than a search. The "too little context" failure is
    not missing content - it is missing pointers.
    """
    if len(memory_core) > MEMORY_CORE_MAX:
        raise ValueError(
            f"memory core is {len(memory_core)} facts, cap is {MEMORY_CORE_MAX}. "
            "Curate it or move facts to retrieval."
        )

    parts = [identity.strip(), "", _section("Voice", examples)]

    if memory_core:
        parts.append(_section("About Callum", "\n".join(f"- {f}" for f in memory_core)))

    # Volatile state never enters conversation history: history is immutable and
    # timeless, so "I'm heading out" reads as equally true three hours later.
    parts.append(_section("Current state", attention_block))

    if task_index:
        parts.append(_section("Active tasks", "\n".join(f"- {t}" for t in task_index)))

    if retrieved:
        parts.append(_section("Recalled", "\n".join(f"- {r}" for r in retrieved)))

    if summary:
        parts.append(_section("Earlier in this conversation", summary))

    system = "\n".join(parts).strip()

    return PromptBundle(
        system=system,
        messages=list(conversation),
        layers={
            "identity": len(identity),
            "examples": len(examples),
            "memory_core": len(memory_core),
            "attention": len(attention_block),
            "task_index": len(task_index),
            "retrieved": len(retrieved or []),
            "conversation": len(conversation),
        },
    )
