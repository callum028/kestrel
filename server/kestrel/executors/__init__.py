"""Executors.

Four kinds, which is what makes the task abstraction real rather than
Claude-Code-shaped with decoration. The interface is deliberately small: take a
brief, emit events, accept input, report needs-input / done / failed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..tasks import Task


class ExecutorKind(StrEnum):
    CLAUDE_CODE = "claude_code"  # code supervision
    KESTREL = "kestrel"  # things the model does directly with tools
    HUMAN = "human"  # commitments - Kestrel tracks and chases
    PHONE = "phone"  # actions dispatched to the Android client


@runtime_checkable
class Executor(Protocol):
    kind: ExecutorKind

    async def start(self, task: Task, brief: str) -> None:
        """Begin work. Emits events; never returns a result directly."""
        ...

    async def send(self, task: Task, message: str) -> None:
        """Deliver input mid-flight - a relayed answer, or a nudge.

        For Claude Code this writes into the same session rather than opening a
        parallel channel, which is why the write path needs a lock.
        """
        ...

    async def stop(self, task: Task, reason: str) -> None:
        """Last rung of the intervention ladder, not the first."""
        ...
