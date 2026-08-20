"""The model interface.

Fifty lines, not a gateway. Its value is having one place to change providers when
a free tier disappears - which it will. Routing is by class of call, not by
preferred provider.

Local inference is not a planned capability: both machines are AMD, so it was
never going to pay off. The interface leaves the door open if that changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class CallClass(StrEnum):
    CONVERSATION = "conversation"  # fast and cheap beats smart
    ROUTINE = "routine"  # routing, classification, summarising
    JUDGEMENT = "judgement"  # where being wrong is silent and costly


@dataclass(frozen=True)
class Message:
    role: str  # system | user | assistant | tool
    content: str


@dataclass(frozen=True)
class ModelResponse:
    text: str
    provider: str
    tool_calls: list[dict[str, Any]] | None = None


class Provider(Protocol):
    name: str

    async def complete(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> ModelResponse: ...


class Router:
    """Routes by call class and injects the identity layer into *every* call.

    The single biggest cause of personality drift is a cheap routing model
    answering something without it, or a provider swap changing the voice
    underneath. So identity is applied here, not by callers.
    """

    def __init__(self, providers: dict[CallClass, Provider], identity: str) -> None:
        self._providers = providers
        self._identity = identity

    async def complete(
        self,
        call_class: CallClass,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        provider = self._providers.get(call_class)
        if provider is None:
            raise KeyError(f"no provider configured for {call_class}")
        return await provider.complete([Message("system", self._identity), *messages], tools)
