"""Microsoft Graph mail reading.

Delegated auth for exactly one mailbox - the user's own - via the OAuth 2.0
device-code flow, requesting `Mail.Read offline_access` and nothing else. The
app registration is a *public client* with device-code enabled, so there is no
client secret to leak; the refresh token obtained by `kestrel.mail_auth` is the
only long-lived credential, stored on disk `0600` next to the API token
(`auth.py`'s `token_path`) because it carries the same weight - anyone who
reads it can read the whole mailbox until it is revoked in Entra.

Endpoints are Microsoft's current ones, not guessed:
- device code + token: the v2.0 identity platform endpoints under
  `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/...`
  (see "Microsoft identity platform and the OAuth 2.0 device authorization
  grant flow").
- mail: Microsoft Graph v1.0, `GET /me/messages` and `GET /me/messages/{id}`
  (see "List messages" / "Get message" in the Graph API reference).

**App registration Callum needs to create** (documented here, not just in a
chat message, because it is a one-time setup step with no code path to derive
it from): Entra ID -> App registrations -> New registration, single tenant,
"Public client/native (mobile & desktop)" redirect platform (no redirect URI
needed for device code), then Authentication -> "Allow public client flows" =
Yes, then API permissions -> Microsoft Graph -> Delegated -> `Mail.Read` and
`offline_access` -> grant admin consent (he is effectively the tenant admin).
`KESTREL_MAIL_TENANT_ID` and `KESTREL_MAIL_CLIENT_ID` (see `config.py`) are the
tenant and application (client) IDs from that registration's Overview page.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

import httpx

from .mail import Attachment, MailMessage, MailSummary

AUTHORITY = "https://login.microsoftonline.com"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
SCOPE = "Mail.Read offline_access"

# Refresh a little before Graph says the access token actually expires, so a
# slow request never straddles the boundary and gets a 401 mid-call.
EXPIRY_SKEW_SECONDS = 60


class GraphAuthError(RuntimeError):
    """Auth failed in a way that needs a human - a missing or revoked refresh
    token. The message always says the exact command to fix it, matching how
    `SessionHostUnavailable` states what to do rather than just that something
    is wrong."""


@dataclass(frozen=True)
class GraphMailConfig:
    tenant_id: str
    client_id: str
    token_path: Path
    scope: str = SCOPE


class TokenStore:
    """The refresh token on disk. A single JSON file, `0600`, holding only the
    refresh token and when it was written - never the access token, which is
    short-lived and kept in memory only."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> str | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text())["refresh_token"]

    def save(self, refresh_token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"refresh_token": refresh_token, "saved_at": datetime.now(UTC).isoformat()})
        )
        self.path.chmod(0o600)


class _TextExtractor(HTMLParser):
    """Minimal HTML -> readable text. No dependency beyond the stdlib: email
    bodies need reading, not rendering, and a full parser (bs4/lxml) is more
    than that job justifies. `<br>`/block tags become newlines, `<script>` and
    `<style>` are dropped, entities are decoded by the base class."""

    _BLOCK_TAGS: ClassVar[set[str]] = {
        "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    }
    _SKIP_TAGS: ClassVar[set[str]] = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "br" or tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse the blank-line noise block tags leave behind without
        # touching intentional paragraph breaks.
        lines = [line.strip() for line in raw.splitlines()]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


class GraphMailReader:
    """`MailReader` backed by live Microsoft Graph calls. Owns nothing durable
    except the refresh token file - access tokens live in memory for the
    process lifetime only."""

    def __init__(self, config: GraphMailConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._tokens = TokenStore(config.token_path)
        self._client = client or httpx.AsyncClient()
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- token handling -----------------------------------------------------

    async def _access_token_value(self) -> str:
        if self._access_token and time.monotonic() < self._access_token_expires_at:
            return self._access_token

        refresh_token = self._tokens.load()
        if refresh_token is None:
            raise GraphAuthError(
                "no mail credentials stored - run `python -m kestrel.mail_auth` once to "
                "authorise Kestrel against this mailbox"
            )

        resp = await self._client.post(
            f"{AUTHORITY}/{self._config.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self._config.client_id,
                "refresh_token": refresh_token,
                "scope": self._config.scope,
            },
        )
        body = resp.json()
        if resp.status_code != 200:
            # Entra revokes the refresh token if Callum removes consent, or
            # after long inactivity - this must say so, not surface as a bare
            # 400 from deep inside a mail query.
            raise GraphAuthError(
                f"refreshing the mail token failed ({body.get('error', resp.status_code)}) - "
                "re-run `python -m kestrel.mail_auth`"
            )

        self._access_token = body["access_token"]
        self._access_token_expires_at = time.monotonic() + body["expires_in"] - EXPIRY_SKEW_SECONDS
        # Entra may rotate the refresh token on use; persist whichever is current.
        self._tokens.save(body.get("refresh_token", refresh_token))
        return self._access_token

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict:
        token = await self._access_token_value()
        resp = await self._client.get(
            f"{GRAPH_ROOT}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()

    # --- MailReader -----------------------------------------------------------

    async def recent(
        self,
        limit: int = 10,
        since: datetime | None = None,
        sender: str | None = None,
        query: str | None = None,
    ) -> list[MailSummary]:
        select = "id,subject,from,receivedDateTime,bodyPreview"
        params: dict[str, str] = {"$top": str(limit), "$select": select}

        filters = []
        if since is not None:
            filters.append(f"receivedDateTime ge {since.astimezone(UTC).isoformat()}")
        if sender is not None:
            filters.append(f"from/emailAddress/address eq '{sender}'")

        if query is not None:
            # $search and $orderby cannot be combined in the same Graph
            # request, and $search needs this header - see "Search for
            # messages" in the Graph API reference.
            params["$search"] = f'"{query}"'
            headers = {"ConsistencyLevel": "eventual"}
        else:
            params["$orderby"] = "receivedDateTime desc"
            headers = {}
        if filters:
            params["$filter"] = " and ".join(filters)

        token = await self._access_token_value()
        resp = await self._client.get(
            f"{GRAPH_ROOT}/me/messages",
            params=params,
            headers={"Authorization": f"Bearer {token}", **headers},
        )
        resp.raise_for_status()
        return [_to_summary(m) for m in resp.json().get("value", [])]

    async def get(self, message_id: str) -> MailMessage | None:
        select = "id,subject,from,receivedDateTime,body"
        try:
            data = await self._get(
                f"/me/messages/{message_id}",
                params={"$select": select, "$expand": "attachments($select=name)"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return _to_message(data)


def _to_summary(data: dict) -> MailSummary:
    return MailSummary(
        id=data["id"],
        sender=_address(data.get("from")),
        subject=data.get("subject") or "(no subject)",
        received=datetime.fromisoformat(data["receivedDateTime"]),
        preview=data.get("bodyPreview", ""),
    )


def _to_message(data: dict) -> MailMessage:
    body = data.get("body") or {}
    content = body.get("content", "")
    body_text = html_to_text(content) if body.get("contentType") == "html" else content
    attachments = [Attachment(name=a["name"]) for a in data.get("attachments", [])]
    return MailMessage(
        id=data["id"],
        sender=_address(data.get("from")),
        subject=data.get("subject") or "(no subject)",
        received=datetime.fromisoformat(data["receivedDateTime"]),
        body_text=body_text,
        attachments=attachments,
    )


def _address(frm: dict | None) -> str:
    if not frm:
        return "(unknown sender)"
    email = frm.get("emailAddress", {})
    name = email.get("name")
    address = email.get("address", "")
    return f"{name} <{address}>" if name else address
