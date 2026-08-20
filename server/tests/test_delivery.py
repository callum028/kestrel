from datetime import UTC, datetime, timedelta

import pytest

from kestrel.attention import Channel, Focus, Signals, Urgency, compute
from kestrel.delivery import DeliveryTracker, for_voice

NOW = datetime(2026, 8, 20, 14, 38, tzinfo=UTC)


@pytest.fixture
def deliveries(conn, log):
    return DeliveryTracker(conn, log)


def at_desk(**kw):
    base = {
        "now": NOW,
        "heartbeat_at": NOW - timedelta(seconds=5),
        "app_open": True,
        "app_focused": True,
    }
    base.update(kw)
    return compute(Signals(**base))


def away(**kw):
    base = {"now": NOW, "heartbeat_at": None, "phone_on_tailnet": True}
    base.update(kw)
    return compute(Signals(**base))


def test_sends_to_one_channel_not_several(deliveries, log):
    d = deliveries.send(
        "KES-31 merged", "Merged and green on dev.", Urgency.NORMAL, at_desk(), now=NOW
    )
    assert d.channel is Channel.INLINE
    assert len(deliveries.pending()) == 1


def test_a_question_about_the_task_on_screen_lands_inline(deliveries):
    state = at_desk(focus=Focus(task_handle="KES-31", pane="diff"))
    d = deliveries.send(
        "retry limit?",
        "Per-request or per-session?",
        Urgency.BLOCKING,
        state,
        about_task="KES-31",
        now=NOW,
    )
    assert d.channel is Channel.INLINE


def test_blocking_while_away_rings(deliveries):
    d = deliveries.send("stopped", "Needs a decision.", Urgency.BLOCKING, away(), now=NOW)
    assert d.channel is Channel.CALL


def test_unacknowledged_messages_climb_a_rung(deliveries):
    d = deliveries.send("KES-31 merged", "Merged.", Urgency.NORMAL, at_desk(), now=NOW)
    assert deliveries.due_for_escalation(NOW + timedelta(seconds=30)) == []

    due = deliveries.due_for_escalation(NOW + timedelta(minutes=3))
    assert [x.id for x in due] == [d.id]

    escalated = deliveries.escalate(d.id, NOW + timedelta(minutes=3))
    assert escalated.channel is Channel.DESKTOP_NOTIFICATION
    assert escalated.escalations == 1


def test_an_observation_can_never_climb_to_a_call(deliveries):
    d = deliveries.send("stale project", "Untouched 11 days.", Urgency.OBSERVATION, away(), now=NOW)
    assert d.channel is Channel.PHONE_NOTIFICATION
    assert deliveries.escalate(d.id, NOW + timedelta(hours=4)) is None


def test_acknowledging_anywhere_clears_it_everywhere(deliveries):
    d = deliveries.send("KES-31 merged", "Merged.", Urgency.NORMAL, at_desk(), now=NOW)
    deliveries.acknowledge(d.id, on="desktop", now=NOW + timedelta(seconds=20))

    assert deliveries.get(d.id).acknowledged
    assert deliveries.pending() == []
    assert deliveries.due_for_escalation(NOW + timedelta(hours=1)) == []


def test_acknowledging_twice_keeps_the_first_answer(deliveries):
    d = deliveries.send("KES-31 merged", "Merged.", Urgency.NORMAL, at_desk(), now=NOW)
    deliveries.acknowledge(d.id, on="desktop", now=NOW + timedelta(seconds=20))
    deliveries.acknowledge(d.id, on="phone", now=NOW + timedelta(minutes=5))
    assert deliveries.get(d.id).acked_on == "desktop"


def test_voice_gets_two_sentences_and_stops():
    body = (
        "KES-31 is merged and green on dev. Nothing needs you. "
        "The diff touched four files and the refresh path now bounces to login."
    )
    spoken = for_voice(body)
    assert spoken == "KES-31 is merged and green on dev. Nothing needs you."
    assert len(spoken) < len(body)
