import stat
from datetime import UTC, datetime, timedelta

import pytest
from pywebpush import WebPushException

import kestrel.push as push_mod
from kestrel.attention import Channel, Signals, Urgency, compute
from kestrel.delivery import DeliveryTracker
from kestrel.push import PushSender, PushSubscriptionStore, load_or_create_vapid_keys

NOW = datetime(2026, 8, 20, 14, 38, tzinfo=UTC)


def away(**kw):
    base = {"now": NOW, "heartbeat_at": None, "phone_on_tailnet": True}
    base.update(kw)
    return compute(Signals(**base))


@pytest.fixture
def subscriptions(conn, log):
    return PushSubscriptionStore(conn, log)


@pytest.fixture
def keys(tmp_path):
    return load_or_create_vapid_keys(tmp_path / "vapid_private_key.pem")


@pytest.fixture
def sender(keys, subscriptions, log):
    return PushSender(keys, subscriptions, log)


def test_the_vapid_key_is_generated_once_and_stored_0600(tmp_path):
    path = tmp_path / "vapid_private_key.pem"
    first = load_or_create_vapid_keys(path)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    second = load_or_create_vapid_keys(path)
    assert second.public_key_b64 == first.public_key_b64


def test_subscriptions_are_stored_and_listed(subscriptions):
    subscriptions.add("https://push.example/abc", "p256dh-key", "auth-key")
    all_subs = subscriptions.all()
    assert len(all_subs) == 1
    assert all_subs[0].endpoint == "https://push.example/abc"


def test_resubscribing_the_same_endpoint_updates_rather_than_duplicates(subscriptions):
    subscriptions.add("https://push.example/abc", "old-key", "old-auth")
    subscriptions.add("https://push.example/abc", "new-key", "new-auth")
    all_subs = subscriptions.all()
    assert len(all_subs) == 1
    assert all_subs[0].p256dh == "new-key"


def test_removing_an_unknown_endpoint_says_so(subscriptions):
    assert subscriptions.remove("https://push.example/nope") is False


def test_a_successful_send_is_logged(monkeypatch, sender, subscriptions, log):
    subscriptions.add("https://push.example/abc", "p256dh-key", "auth-key")
    calls = []
    monkeypatch.setattr(push_mod, "webpush", lambda **kw: calls.append(kw))

    sender.send(subscriptions.all()[0], {"title": "hi"})

    assert len(calls) == 1
    kinds = [e.kind for e in log.since(0)]
    assert "channel.push_sent" in [str(k) for k in kinds]


def test_a_gone_subscription_is_dropped_not_retried(monkeypatch, sender, subscriptions):
    subscriptions.add("https://push.example/abc", "p256dh-key", "auth-key")

    class FakeResponse:
        status_code = 410

    def fail(**kw):
        raise WebPushException("gone", response=FakeResponse())

    monkeypatch.setattr(push_mod, "webpush", fail)
    sender.send(subscriptions.all()[0], {"title": "hi"})

    assert subscriptions.all() == []


def test_a_server_error_is_logged_but_the_subscription_survives(
    monkeypatch, sender, subscriptions, log
):
    subscriptions.add("https://push.example/abc", "p256dh-key", "auth-key")

    class FakeResponse:
        status_code = 500

    def fail(**kw):
        raise WebPushException("server error", response=FakeResponse())

    monkeypatch.setattr(push_mod, "webpush", fail)
    sender.send(subscriptions.all()[0], {"title": "hi"})

    assert len(subscriptions.all()) == 1
    kinds = [str(e.kind) for e in log.since(0)]
    assert "channel.push_failed" in kinds


def test_a_delivery_that_lands_on_phone_notification_triggers_a_push(
    monkeypatch, conn, log, subscriptions, sender
):
    subscriptions.add("https://push.example/abc", "p256dh-key", "auth-key")
    sent = []
    monkeypatch.setattr(sender, "send_to_all", lambda payload: sent.append(payload))

    deliveries = DeliveryTracker(conn, log, on_phone_notify=sender.notify_delivery)
    d = deliveries.send("stale project", "Untouched 11 days.", Urgency.OBSERVATION, away(), now=NOW)

    assert d.channel is Channel.PHONE_NOTIFICATION
    assert len(sent) == 1
    assert sent[0]["title"] == "stale project"


def test_an_inline_delivery_never_triggers_a_push(monkeypatch, conn, log, subscriptions, sender):
    sent = []
    monkeypatch.setattr(sender, "send_to_all", lambda payload: sent.append(payload))
    deliveries = DeliveryTracker(conn, log, on_phone_notify=sender.notify_delivery)

    state = compute(Signals(now=NOW, heartbeat_at=NOW, app_open=True, app_focused=True))
    deliveries.send("KES-31 merged", "Merged.", Urgency.NORMAL, state, now=NOW)

    assert sent == []


def test_escalating_into_phone_notification_also_pushes(
    monkeypatch, conn, log, subscriptions, sender
):
    sent = []
    monkeypatch.setattr(sender, "send_to_all", lambda payload: sent.append(payload))
    deliveries = DeliveryTracker(conn, log, on_phone_notify=sender.notify_delivery)

    state = compute(
        Signals(now=NOW, heartbeat_at=NOW, app_open=True, app_focused=False, phone_on_tailnet=True)
    )
    d = deliveries.send("KES-31 merged", "Merged.", Urgency.NORMAL, state, now=NOW)
    assert d.channel is Channel.DESKTOP_NOTIFICATION
    assert sent == []

    deliveries.escalate(d.id, NOW + timedelta(minutes=3))
    assert len(sent) == 1
