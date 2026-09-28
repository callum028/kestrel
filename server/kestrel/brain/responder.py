"""`BrainResponder` - the real `Responder` for `conversation.py`.

Per that module's brain-step contract: one method, `respond(text, history) ->
str`. Everything about *how* the reply is produced - context assembly,
retrieval, which model, which tools - lives here, not in `conversation.py`.

Failure is not swallowed here: a `BrainError` from the runner propagates to
the caller (`ConversationStore._respond_and_store`), which is what turns a
dead brain into a visible system message and a logged event rather than
silence - see `conversation.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .context_assembly import build_prompt, render_for_headless
from .routing import DEFAULT_MODEL_NAMES, ModelNames, Turn, decide_model
from .runner import BrainRunner, MCPServerSpec

if TYPE_CHECKING:  # pragma: no cover
    from ..conversation import ConversationMessage
    from ..memory import MemoryStore
    from ..tasks import Task

# The brain's own tools live behind one MCP server, so this is the entire
# allowlist - no built-in ever appears here (see runner.py's
# DISALLOWED_BUILTIN_TOOLS, which is enforced regardless of this list).
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ("mcp__kestrel__*",)


@dataclass
class BrainResponder:
    runner: BrainRunner
    identity_dir: Path
    memory: MemoryStore
    state_block: Callable[[], str]
    active_tasks: Callable[[], list[Task]]
    mcp_servers: list[MCPServerSpec] = field(default_factory=list)
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    project: str | None = None
    models: ModelNames = field(default_factory=lambda: DEFAULT_MODEL_NAMES)

    name = "brain"

    async def respond(self, text: str, history: list[ConversationMessage]) -> str:
        bundle = build_prompt(
            identity_dir=self.identity_dir,
            memory=self.memory,
            attention_block=self.state_block(),
            active_tasks=self.active_tasks(),
            history=history,
            message=text,
            project=self.project,
        )
        system_prompt, query = render_for_headless(bundle)
        model = decide_model(Turn(text=text, history=history), self.models)
        result = await self.runner.run(
            system_prompt=system_prompt,
            user_message=query,
            model=model,
            allowed_tools=list(self.allowed_tools),
            mcp_servers=self.mcp_servers,
        )
        return result.text
