"""Web Push - the channel that gets a notification to the phone with the app
closed, or even the browser not running.

Standard Web Push (RFC 8030) with VAPID (RFC 8292) application-server
identification. Uses `pywebpush` rather than hand-rolling aes128gcm payload
encryption (RFC 8188) and the ECDH key agreement underneath it - both are easy
to get subtly wrong, and pywebpush (with py_vapid) is the de facto standard
for this in Python; there is no ergonomic gain to writing it by hand, only
risk.

The VAPID keypair is generated once per install and stored 0600 in the data
directory, mirroring `auth.load_or_create_token` exactly - same reasoning:
regenerating it invalidates every subscription silently, so load-or-create is
the only safe shape.

This module owns subscriptions and the actual send; it does not decide *when*
to push. That decision is `attention.choose_channel`'s (a delivery's channel
comes out as `PHONE_NOTIFICATION`) - `Runtime` wires a `PushSender.send_delivery`
callback into `DeliveryTracker` so a delivery landing on that channel actually
reaches the phone, on top of being visible next time a client polls
`/deliveries`. One delivery, one channel, so this never fires alongside a
desktop notification for the same event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid01
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush

from .db import Database
from .events import EventKind, EventLog

if TYPE_CHECKING:
    from .delivery import Delivery

logger = logging.getLogger("kestrel.push")

# VAPID requires *a* contact URI in the JWT claims so a push service operator
# can reach out if a sender is misbehaving. It is never dialled by the
# protocol itself, so a real inbox is not required - just a stable identifier.
VAPID_SUBJECT = "mailto:kestrel@localhost"


@dataclass(frozen=True)
class VapidKeys:
    private_key_path: Path
    public_key_b64: str


def load_or_create_vapid_keys(path: Path) -> VapidKeys:
    """One keypair per install. `Vapid01.from_file` already loads-or-generates,
    but it does not set file permissions - the private key is as sensitive as
    the API token, so this chmods it every time (cheap, and self-healing if
    something else created the file with looser permissions)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    vapid = Vapid01.from_file(str(path))
    path.chmod(0o600)
    public_bytes = vapid.public_key.public_bytes(
        encoding=Encoding.X962, format=PublicFormat.UncompressedPoint
    )
    return VapidKeys(private_key_path=path, public_key_b64=b64urlencode(public_bytes))


@dataclass(frozen=True)
class PushSubscription:
    endpoint: str
    p256dh: str
    auth: str

    def to_webpush_dict(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


def _to_subscription(row: sqlite3.Row) -> PushSubscription:
    return PushSubscription(endpoint=row["endpoint"], p256dh=row["p256dh"], auth=row["auth"])


class PushSubscriptionStore:
    def __init__(self, conn: Database, log: EventLog) -> None:
        self._conn = conn
        self._log = log

    def add(self, endpoint: str, p256dh: str, auth: str, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self._conn.execute(
            """INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth""",
            (endpoint, p256dh, auth, now.isoformat()),
        )
        self._log.append(EventKind.PUSH_SUBSCRIBED, "client", {"endpoint": endpoint})

    def remove(self, endpoint: str) -> bool:
        cur = self._conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        if cur.rowcount:
            self._log.append(EventKind.PUSH_UNSUBSCRIBED, "client", {"endpoint": endpoint})
        return bool(cur.rowcount)

    def all(self) -> list[PushSubscription]:
        rows = self._conn.execute("SELECT * FROM push_subscriptions").fetchall()
        return [_to_subscription(r) for r in rows]


class PushSender:
    """The actual HTTP call to the push service. Synchronous (pywebpush wraps
    `requests`) - callers from async code should offload it, which is exactly
    what `PushSender.notify_delivery` does when a running loop is calling it,
    so the tick loop's event loop is never blocked waiting on a push service."""

    def __init__(
        self, keys: VapidKeys, subscriptions: PushSubscriptionStore, log: EventLog
    ) -> None:
        self._keys = keys
        self._subscriptions = subscriptions
        self._log = log

    def send(self, subscription: PushSubscription, payload: dict[str, Any]) -> bool:
        try:
            webpush(
                subscription_info=subscription.to_webpush_dict(),
                data=json.dumps(payload),
                vapid_private_key=str(self._keys.private_key_path),
                vapid_claims={"sub": VAPID_SUBJECT},
            )
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410):
                # Gone: the browser unsubscribed or the install was wiped.
                # Not an error worth surfacing - just stop trying that endpoint.
                self._subscriptions.remove(subscription.endpoint)
            else:
                logger.warning("push failed (%s): %s", status, exc)
                self._log.append(
                    EventKind.PUSH_FAILED,
                    "kestrel",
                    {"endpoint": subscription.endpoint, "status": status, "error": str(exc)},
                )
            return False
        self._log.append(EventKind.PUSH_SENT, "kestrel", {"endpoint": subscription.endpoint})
        return True

    def send_to_all(self, payload: dict[str, Any]) -> None:
        for subscription in self._subscriptions.all():
            self.send(subscription, payload)

    def notify_delivery(self, delivery: Delivery) -> None:
        """Wired into `DeliveryTracker` as the phone-notification channel.
        Content mirrors what the desktop already shows - one conversation,
        rendered differently per channel, never a different message.

        `DeliveryTracker` calls this from both plain sync endpoints (FastAPI's
        threadpool - blocking here is harmless) and the async tick loop. In the
        latter case a blocking HTTP call to the push service would stall every
        other coroutine, so this offloads to the default executor whenever a
        loop is actually running underneath it.
        """
        payload = {
            "title": delivery.subject,
            "body": delivery.body,
            "urgency": str(delivery.urgency),
            "delivery_id": delivery.id,
            "task_id": delivery.task_id,
        }
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.run_in_executor(None, self.send_to_all, payload)
        else:
            self.send_to_all(payload)
