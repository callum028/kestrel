import json
import stat

import httpx
import pytest

from kestrel.graph_mail import (
    GraphAuthError,
    GraphMailConfig,
    GraphMailReader,
    TokenStore,
    html_to_text,
)

TENANT = "test-tenant"
CLIENT = "test-client"


def token_response(access="access-1", refresh="refresh-1"):
    return httpx.Response(
        200, json={"access_token": access, "refresh_token": refresh, "expires_in": 3600}
    )


def make_reader(tmp_path, handler, refresh_token="refresh-0"):
    token_path = tmp_path / "mail_token"
    if refresh_token is not None:
        TokenStore(token_path).save(refresh_token)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = GraphMailConfig(tenant_id=TENANT, client_id=CLIENT, token_path=token_path)
    return GraphMailReader(config, client=client)


# --- token handling ----------------------------------------------------------


async def test_no_stored_token_raises_a_message_naming_the_fix(tmp_path):
    reader = make_reader(tmp_path, lambda r: token_response(), refresh_token=None)
    with pytest.raises(GraphAuthError, match="mail_auth"):
        await reader.recent()


async def test_refreshes_using_the_stored_refresh_token(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/oauth2/v2.0/token"):
            body = dict(p.split("=") for p in request.content.decode().split("&"))
            assert body["grant_type"] == "refresh_token"
            assert body["refresh_token"] == "refresh-0"
            assert body["client_id"] == CLIENT
            return token_response()
        return httpx.Response(200, json={"value": []})

    reader = make_reader(tmp_path, handler)
    await reader.recent()

    token_calls = [c for c in calls if c.url.path.endswith("/oauth2/v2.0/token")]
    assert len(token_calls) == 1
    assert f"/{TENANT}/oauth2/v2.0/token" in str(token_calls[0].url)


async def test_access_token_is_cached_across_calls(tmp_path):
    token_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            token_calls.append(request)
            return token_response()
        return httpx.Response(200, json={"value": []})

    reader = make_reader(tmp_path, handler)
    await reader.recent()
    await reader.recent()

    assert len(token_calls) == 1  # second call reused the cached access token


async def test_a_rotated_refresh_token_is_persisted(tmp_path):
    token_path = tmp_path / "mail_token"
    TokenStore(token_path).save("refresh-0")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return token_response(refresh="refresh-1-rotated")
        return httpx.Response(200, json={"value": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = GraphMailConfig(tenant_id=TENANT, client_id=CLIENT, token_path=token_path)
    reader = GraphMailReader(config, client=client)
    await reader.recent()

    assert TokenStore(token_path).load() == "refresh-1-rotated"


async def test_a_revoked_refresh_token_raises_a_readable_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    reader = make_reader(tmp_path, handler)
    with pytest.raises(GraphAuthError, match="mail_auth"):
        await reader.recent()


def test_token_file_is_readable_only_by_the_owner(tmp_path):
    path = tmp_path / "mail_token"
    TokenStore(path).save("secret-refresh-token")

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_token_file_never_stores_the_access_token(tmp_path):
    path = tmp_path / "mail_token"
    TokenStore(path).save("refresh-only")
    stored = json.loads(path.read_text())
    assert set(stored) == {"refresh_token", "saved_at"}


# --- request shapes ----------------------------------------------------------


async def test_recent_requests_top_orderby_and_select(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return token_response()
        seen["params"] = dict(request.url.params)
        seen["path"] = request.url.path
        return httpx.Response(200, json={"value": []})

    reader = make_reader(tmp_path, handler)
    await reader.recent(limit=5)

    assert seen["path"] == "/v1.0/me/messages"
    assert seen["params"]["$top"] == "5"
    assert seen["params"]["$orderby"] == "receivedDateTime desc"
    assert "id" in seen["params"]["$select"]


async def test_recent_with_sender_and_since_builds_a_filter(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return token_response()
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"value": []})

    from datetime import UTC, datetime

    reader = make_reader(tmp_path, handler)
    await reader.recent(sender="bob@work.com", since=datetime(2026, 1, 1, tzinfo=UTC))

    assert "from/emailAddress/address eq 'bob@work.com'" in seen["params"]["$filter"]
    assert "receivedDateTime ge" in seen["params"]["$filter"]


async def test_recent_with_query_uses_search_not_orderby(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return token_response()
        seen["params"] = dict(request.url.params)
        seen["headers"] = request.headers
        return httpx.Response(200, json={"value": []})

    reader = make_reader(tmp_path, handler)
    await reader.recent(query="invoice")

    assert seen["params"]["$search"] == '"invoice"'
    assert "$orderby" not in seen["params"]
    assert seen["headers"]["ConsistencyLevel"] == "eventual"


async def test_recent_parses_returned_messages(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return token_response()
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "abc",
                        "subject": "Hello",
                        "from": {"emailAddress": {"name": "Alice", "address": "alice@x.com"}},
                        "receivedDateTime": "2026-01-05T10:00:00Z",
                        "bodyPreview": "Hi there",
                    }
                ]
            },
        )

    reader = make_reader(tmp_path, handler)
    results = await reader.recent()
    assert len(results) == 1
    assert results[0].id == "abc"
    assert results[0].sender == "Alice <alice@x.com>"
    assert results[0].preview == "Hi there"


async def test_get_requests_the_message_with_body_and_attachment_names_only(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return token_response()
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "id": "abc",
                "subject": "Hello",
                "from": {"emailAddress": {"name": "Alice", "address": "alice@x.com"}},
                "receivedDateTime": "2026-01-05T10:00:00Z",
                "body": {"contentType": "text", "content": "Plain body"},
                "attachments": [{"name": "invoice.pdf"}],
            },
        )

    reader = make_reader(tmp_path, handler)
    result = await reader.get("abc")

    assert seen["path"] == "/v1.0/me/messages/abc"
    assert "attachments" in seen["params"]["$expand"]
    assert result.body_text == "Plain body"
    assert [a.name for a in result.attachments] == ["invoice.pdf"]


async def test_get_converts_html_bodies_to_text(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return token_response()
        return httpx.Response(
            200,
            json={
                "id": "abc",
                "subject": "Hello",
                "from": {"emailAddress": {"address": "alice@x.com"}},
                "receivedDateTime": "2026-01-05T10:00:00Z",
                "body": {"contentType": "html", "content": "<p>Hi <b>there</b></p><p>Bye</p>"},
                "attachments": [],
            },
        )

    reader = make_reader(tmp_path, handler)
    result = await reader.get("abc")
    assert result.body_text == "Hi there\n\nBye"


async def test_get_returns_none_for_a_404(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return token_response()
        return httpx.Response(404, json={"error": {"message": "not found"}})

    reader = make_reader(tmp_path, handler)
    assert await reader.get("missing") is None


# --- html_to_text -------------------------------------------------------------


def test_html_to_text_strips_tags_and_keeps_words():
    assert html_to_text("<p>Hello <b>world</b></p>") == "Hello world"


def test_html_to_text_converts_br_and_block_tags_to_newlines():
    assert html_to_text("Line one<br>Line two<div>Line three</div>") == (
        "Line one\nLine two\nLine three"
    )


def test_html_to_text_drops_script_and_style_content():
    html = "<style>.x{color:red}</style><p>Visible</p><script>evil()</script>"
    assert html_to_text(html) == "Visible"


def test_html_to_text_decodes_entities():
    assert html_to_text("<p>Ben &amp; Jerry&#39;s</p>") == "Ben & Jerry's"
