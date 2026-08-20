from datetime import UTC, datetime, timedelta

from kestrel.attention import (
    Channel,
    Focus,
    Presence,
    Signals,
    Urgency,
    choose_channel,
    compute,
    escalate,
)

NOW = datetime(2026, 8, 20, 14, 38, tzinfo=UTC)


def signals(**kw) -> Signals:
    base = {"now": NOW, "heartbeat_at": NOW - timedelta(seconds=5)}
    base.update(kw)
    return Signals(**base)


def test_dead_heartbeat_means_away_regardless_of_app_state():
    s = signals(heartbeat_at=NOW - timedelta(minutes=10), app_open=True, app_focused=True)
    assert compute(s).presence is Presence.AWAY


def test_open_but_unfocused_with_recent_input_is_still_at_desk():
    s = signals(app_open=True, app_focused=False, last_input_at=NOW - timedelta(seconds=30))
    assert compute(s).presence is Presence.AT_DESK


def test_open_but_idle_is_nearby_not_at_desk():
    s = signals(app_open=True, app_focused=False, last_input_at=NOW - timedelta(minutes=20))
    assert compute(s).presence is Presence.NEARBY


def test_stated_away_overrides_sensors_until_it_expires():
    s = signals(app_focused=True, stated_away_until=NOW + timedelta(hours=1))
    assert compute(s).presence is Presence.AWAY


def test_question_about_the_task_on_screen_arrives_inline():
    s = signals(app_open=True, app_focused=True, focus=Focus(task_handle="KES-31", pane="diff"))
    state = compute(s)
    assert choose_channel(state, Urgency.NORMAL, focused_on="KES-31") is Channel.INLINE


def test_blocking_while_away_rings():
    s = signals(heartbeat_at=None, phone_on_tailnet=True)
    state = compute(s)
    assert choose_channel(state, Urgency.BLOCKING) is Channel.CALL


def test_observation_while_away_never_rings():
    s = signals(heartbeat_at=None, phone_on_tailnet=True)
    state = compute(s)
    assert choose_channel(state, Urgency.OBSERVATION) is Channel.PHONE_NOTIFICATION


def test_speech_suppressed_during_a_meeting():
    assert compute(signals(calendar_busy=True)).speech_suppressed is True


def test_escalation_ceiling_holds_for_observations():
    assert escalate(Channel.DESKTOP_NOTIFICATION, Urgency.OBSERVATION) is Channel.PHONE_NOTIFICATION
    assert escalate(Channel.PHONE_NOTIFICATION, Urgency.OBSERVATION) is None
    assert escalate(Channel.PHONE_NOTIFICATION, Urgency.BLOCKING) is Channel.CALL
