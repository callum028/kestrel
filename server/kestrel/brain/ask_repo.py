"""`ask_repo` - a read-only question about a project's code.

A separate headless `claude -p` run, always Sonnet (a code question is a
judgement call, not a lookup), with only read tools and no MCP servers at
all - it needs nothing from Kestrel's own tool surface, only the repository
in front of it.

The one deliberate exception to `BrainRunner`'s "never the caller's own
checkout" rule: `cwd` here *is* the project's real repository path, not an
empty work_dir. Read-only tools are what keeps that safe - there is nothing
in `READ_ONLY_TOOLS` that can change a file, and the built-in disallow list
(`runner.DISALLOWED_BUILTIN_TOOLS`) still blocks Bash/Edit/Write regardless
of what's passed here.
"""

from __future__ import annotations

from pathlib import Path

from .runner import BrainRunner

READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")

_SYSTEM_PROMPT = (
    "Answer a read-only question about the code in this repository. You have "
    "Read, Grep and Glob only - no edits, no shell, no network. Be precise: "
    "cite file paths and line numbers where you can, and say plainly when the "
    "code does not answer the question rather than guessing."
)


async def ask_repo(question: str, *, runner: BrainRunner, repo_path: Path) -> str:
    result = await runner.run(
        system_prompt=_SYSTEM_PROMPT,
        user_message=question,
        model="sonnet",
        allowed_tools=list(READ_ONLY_TOOLS),
        cwd=repo_path,
    )
    return result.text
