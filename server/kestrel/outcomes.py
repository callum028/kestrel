"""Discriminated results.

Every instruction ends in a state. There is deliberately no shape of Outcome that
means "nothing happened" - a miss cannot be swallowed silently, which is the
failure that made v1 feel broken (a mis-heard contact name that simply gave up).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


class Ok(BaseModel):
    status: Literal["ok"] = "ok"
    value: Any = None


class NotFound(BaseModel):
    status: Literal["not_found"] = "not_found"
    searched_for: str
    looked_in: str | None = None


class Ambiguous(BaseModel):
    status: Literal["ambiguous"] = "ambiguous"
    searched_for: str
    candidates: list[str]


class Refused(BaseModel):
    status: Literal["refused"] = "refused"
    reason: str


Outcome = Annotated[Union[Ok, NotFound, Ambiguous, Refused], Field(discriminator="status")]
