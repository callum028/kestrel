"""Attention - presence and focus as one subsystem.

Sensors, not beliefs. No model anywhere in this file. Two consumers: the prompt
assembler and the channel router - and the router only works if focus is real
state rather than a string in a context block.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

DESK_IDLE_GRACE = timedelta(minutes=4)
HEARTBEAT_TIMEOUT = timedelta(seconds=90)


class Presence(StrEnum):
    AT_DESK = "at_desk"  # app focused, or recent input
    NEARBY = "nearby"  # machine awake, input a while ago
    AWAY = "away"


class Channel(StrEnum):
    INLINE = "inline"
    DESKTOP_NOTIFICATION = "desktop_notification"
    PHONE_NOTIFICATION = "phone_notification"
    CALL = "call"


class Urgency(StrEnum):
    OBSERVATION = "observation"  # never earns a call, never urgent
    NORMAL = "normal"
    BLOCKING = "blocking"  # work has stopped dead


@dataclass(frozen=True)
class Focus:
    """What is on screen right now. The useful half of the v1 screen-vision idea,
    done structurally: exact rather than approximate, and no vision model."""

    task_handle: str | None = None
    pane: str | None = None  # session | diff | ticket | chat
    selection: str | None = None  # e.g. src/auth/token.ts:40-72

    def describe(self) -> str:
        parts = [p for p in (self.task_handle, self.pane, self.selection) if p]
        return " / ".join(parts) if parts else "none"


@dataclass(frozen=True)
class Signals:
    """Raw client reports. Everything here is observed, never inferred."""

    now: datetime
    app_focused: bool = False
    app_open: bool = False
    last_input_at: datetime | None = None
    heartbeat_at: datetime | None = None
    phone_on_tailnet: bool = False
    voice_session_open: bool = False
    calendar_busy: bool = False
    on_call: bool = False
    audio_route: str | None = None  # aux | bluetooth | speaker
    focus: Focus = Focus()
    stated_away_until: datetime | None = None  # what he says is an override with a TTL


@dataclass(frozen=True)
class AttentionState:
    presence: Presence
    focus: Focus
    app_open: bool
    speech_suppressed: bool
    phone_reachable: bool
    pc_awake: bool

    def describe(self, now: datetime, signals: Signals) -> str:
        """Pre-computed for the prompt. The model reads facts; it never subtracts
        timestamps, because it is bad at that and the transcript is timeless."""
        lines = [f"now: {now.isoformat(timespec='seconds')}"]
        detail = ""
        if signals.last_input_at:
            mins = int((now - signals.last_input_at).total_seconds() // 60)
            detail = f" (last input {mins}m ago)"
        lines.append(f"presence: {self.presence.upper()}{detail}")
        lines.append(f"focus: {self.focus.describe()}")
        if self.speech_suppressed:
            lines.append("speech: suppressed")
        return "\n".join(lines)


def compute(signals: Signals) -> AttentionState:
    now = signals.now
    pc_awake = signals.heartbeat_at is not None and now - signals.heartbeat_at < HEARTBEAT_TIMEOUT

    stated_away = signals.stated_away_until is not None and now < signals.stated_away_until
    recent_input = (
        signals.last_input_at is not None and now - signals.last_input_at < DESK_IDLE_GRACE
    )

    # An open-but-unfocused app says nothing about whether a human is there;
    # idle time is what disambiguates "in VS Code next door" from "in the kitchen".
    if stated_away or not pc_awake:
        presence = Presence.AWAY
    elif signals.app_focused or recent_input:
        presence = Presence.AT_DESK
    else:
        presence = Presence.NEARBY if signals.app_open else Presence.AWAY

    return AttentionState(
        presence=presence,
        focus=signals.focus,
        app_open=signals.app_open,
        speech_suppressed=signals.on_call or signals.calendar_busy,
        phone_reachable=signals.phone_on_tailnet,
        pc_awake=pc_awake,
    )


def choose_channel(
    state: AttentionState, urgency: Urgency, focused_on: str | None = None
) -> Channel:
    """Deterministic. Attention decides where it goes; urgency decides how loud.

    If a question concerns the task already on screen, it arrives inline - no
    notification, no buzz. That behaviour is impossible if focus lives only in
    the prompt.
    """
    if state.presence is Presence.AT_DESK:
        if focused_on and state.focus.task_handle == focused_on:
            return Channel.INLINE
        return Channel.INLINE if state.app_open else Channel.DESKTOP_NOTIFICATION

    if state.presence is Presence.NEARBY:
        return Channel.DESKTOP_NOTIFICATION

    if urgency is Urgency.BLOCKING and state.phone_reachable:
        return Channel.CALL
    return Channel.PHONE_NOTIFICATION


def escalate(current: Channel, urgency: Urgency) -> Channel | None:
    """Escalate on silence rather than fanning out. Duplicate notifications train
    you to ignore both. An observation never climbs past a notification."""
    ladder = [
        Channel.INLINE,
        Channel.DESKTOP_NOTIFICATION,
        Channel.PHONE_NOTIFICATION,
        Channel.CALL,
    ]
    ceiling = Channel.PHONE_NOTIFICATION if urgency is not Urgency.BLOCKING else Channel.CALL
    idx = ladder.index(current)
    if current == ceiling or idx + 1 >= len(ladder):
        return None
    nxt = ladder[idx + 1]
    return None if ladder.index(nxt) > ladder.index(ceiling) else nxt
