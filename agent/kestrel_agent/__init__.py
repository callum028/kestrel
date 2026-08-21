"""Kestrel agent - the executor side.

Runs where the code is (WSL), never where the state is. Owns PTYs, git
worktrees, and Claude Code sessions; holds nothing durable, because everything
it learns is streamed to the server as events.

For v0 the server imports this directly, since both run in WSL. When the server
moves to the Pi this package is spawned over stdio instead - the interface is
the same either way, which is the point of keeping it separate now.
"""

__version__ = "0.1.0"
