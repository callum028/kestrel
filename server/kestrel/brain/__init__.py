"""Kestrel's brain - headless `claude -p` as the model behind the one
conversation, plus the pieces around it: model routing, context assembly for
this step, narration, and the `kestrel-mcp` tool server it talks to.

Nothing in this package talks to the Anthropic API directly. Every model call
here bills to Callum's Max subscription through the Claude Code CLI, per
docs/design.md's hard constraint - see `runner.py` for the invocation and the
CLI flags that make it non-interactive, tool-restricted, and free of any
long-lived session.
"""

from __future__ import annotations
