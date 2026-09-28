"""The origin allowlist: dev/Tauri defaults, plus whatever
`KESTREL_ALLOWED_ORIGINS` adds for a tailnet deployment - see
`config.Config.allowed_origins` for where the environment variable is parsed
and threaded through, and `test_cors.py` for the same rule exercised over a
real HTTP app rather than these functions directly.
"""

from fastapi import HTTPException
from fastapi.testclient import TestClient

from kestrel.api import create_app
from kestrel.auth import (
    DEFAULT_ALLOWED_ORIGINS,
    check_request,
    check_websocket,
    parse_allowed_origins,
)
from kestrel.config import Config
from kestrel.runtime import Runtime

TAILNET = "https://kestrel-pi.tailnet-1234.ts.net"
LOCAL_DIRECT = "http://localhost:8099"


def test_parse_allowed_origins_is_additive_to_the_defaults():
    origins = parse_allowed_origins(TAILNET)
    assert origins == DEFAULT_ALLOWED_ORIGINS | {TAILNET}


def test_parse_allowed_origins_handles_several_comma_separated():
    origins = parse_allowed_origins(f"{TAILNET}, https://second.example.ts.net")
    assert TAILNET in origins
    assert "https://second.example.ts.net" in origins


def test_parse_allowed_origins_ignores_blanks_and_whitespace():
    assert parse_allowed_origins("") == DEFAULT_ALLOWED_ORIGINS
    assert parse_allowed_origins(None) == DEFAULT_ALLOWED_ORIGINS
    assert parse_allowed_origins(" , ,") == DEFAULT_ALLOWED_ORIGINS


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class _Addr:
    def __init__(self, host: str | None) -> None:
        self.host = host


class _Url:
    def __init__(self, scheme: str) -> None:
        self.scheme = scheme


class _FakeRequest:
    """Just enough of `fastapi.Request` for `check_request` - including the
    bits `_effective_server_origin` needs: the actual TCP peer (`client`,
    never something a header can forge) and the scheme the ASGI server
    itself accepted the connection on (`url.scheme`, always "http" for this
    loopback-bound process - see auth.py's docstring)."""

    def __init__(
        self,
        headers: dict[str, str],
        client_host: str | None = "203.0.113.5",
        scheme: str = "http",
    ) -> None:
        self.headers = _Headers({k.lower(): v for k, v in headers.items()})
        self.client = _Addr(client_host)
        self.url = _Url(scheme)


class _QueryParams(dict):
    pass


class _FakeWebSocket:
    """Just enough of `fastapi.WebSocket` for `check_websocket`."""

    def __init__(
        self,
        headers: dict[str, str],
        query: dict[str, str],
        client_host: str | None = "203.0.113.5",
        scheme: str = "ws",
    ) -> None:
        self.headers = _Headers({k.lower(): v for k, v in headers.items()})
        self.query_params = _QueryParams(query)
        self.client = _Addr(client_host)
        self.url = _Url(scheme)


TOKEN = "s3cr3t"


def test_check_request_accepts_the_configured_extra_origin():
    origins = parse_allowed_origins(TAILNET)
    request = _FakeRequest({"origin": TAILNET, "authorization": f"Bearer {TOKEN}"})
    check_request(request, TOKEN, origins)  # does not raise


def test_check_request_still_refuses_an_unrecognised_origin():
    origins = parse_allowed_origins(TAILNET)
    request = _FakeRequest(
        {"origin": "https://evil.example.com", "authorization": f"Bearer {TOKEN}"}
    )
    try:
        check_request(request, TOKEN, origins)
        assert False, "expected a 403"
    except HTTPException as exc:
        assert exc.status_code == 403


def test_check_websocket_accepts_the_configured_extra_origin():
    origins = parse_allowed_origins(TAILNET)
    ws = _FakeWebSocket({"origin": TAILNET}, {"token": TOKEN})
    assert check_websocket(ws, TOKEN, origins) is True


def test_check_websocket_still_refuses_an_unrecognised_origin():
    origins = parse_allowed_origins(TAILNET)
    ws = _FakeWebSocket({"origin": "https://evil.example.com"}, {"token": TOKEN})
    assert check_websocket(ws, TOKEN, origins) is False


# --- same-origin (the exact bug the orchestrator hit) -----------------------
# `uvicorn ... --host 127.0.0.1 --port 8099`, opened directly at
# http://localhost:8099/#pair=<token> with no proxy in front at all: the
# browser still sends `Origin: http://localhost:8099` on the pairing
# validation POST, `Host` is `localhost:8099`, and neither is in
# DEFAULT_ALLOWED_ORIGINS or any KESTREL_ALLOWED_ORIGINS a phone-pairing
# deployment would think to set - it has to work with zero configuration.


def test_check_request_accepts_a_direct_same_origin_request_with_no_config():
    request = _FakeRequest(
        {
            "origin": LOCAL_DIRECT,
            "host": "localhost:8099",
            "authorization": f"Bearer {TOKEN}",
        },
        client_host="127.0.0.1",
        scheme="http",
    )
    check_request(request, TOKEN, DEFAULT_ALLOWED_ORIGINS)  # does not raise


def test_check_request_still_refuses_a_mismatched_host_even_from_loopback():
    """The peer being loopback only makes forwarded headers *eligible* for
    trust - it must never be read as "any Origin is fine from here"."""
    request = _FakeRequest(
        {
            "origin": "https://evil.example.com",
            "host": "localhost:8099",
            "authorization": f"Bearer {TOKEN}",
        },
        client_host="127.0.0.1",
        scheme="http",
    )
    try:
        check_request(request, TOKEN, DEFAULT_ALLOWED_ORIGINS)
        assert False, "expected a 403"
    except HTTPException as exc:
        assert exc.status_code == 403


def test_check_request_accepts_the_tailnet_origin_via_forwarded_headers_from_loopback():
    """`tailscale serve` terminates TLS at the tailnet hostname and forwards
    plain HTTP to this loopback port - X-Forwarded-Proto/-Host is how the
    server learns what the browser actually saw, and it only has to be
    trusted because the peer handing them over is this same machine."""
    request = _FakeRequest(
        {
            "origin": TAILNET,
            "host": "127.0.0.1:8099",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "kestrel-pi.tailnet-1234.ts.net",
            "authorization": f"Bearer {TOKEN}",
        },
        client_host="127.0.0.1",
        scheme="http",
    )
    check_request(request, TOKEN, DEFAULT_ALLOWED_ORIGINS)  # does not raise, no config needed


def test_check_request_ignores_forwarded_headers_from_a_non_loopback_peer():
    """The one thing that must never be true: a request arriving from
    somewhere other than this machine forging X-Forwarded-* to impersonate
    an allowed origin. (In practice nothing but loopback can reach this
    port at all - this is defence in depth against a future misconfiguration,
    e.g. `--host 0.0.0.0`.)"""
    request = _FakeRequest(
        {
            "origin": TAILNET,
            "host": "127.0.0.1:8099",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "kestrel-pi.tailnet-1234.ts.net",
            "authorization": f"Bearer {TOKEN}",
        },
        client_host="198.51.100.9",
        scheme="http",
    )
    try:
        check_request(request, TOKEN, DEFAULT_ALLOWED_ORIGINS)
        assert False, "expected a 403"
    except HTTPException as exc:
        assert exc.status_code == 403


def test_check_websocket_accepts_a_direct_same_origin_upgrade_with_no_config():
    ws = _FakeWebSocket(
        {"origin": LOCAL_DIRECT, "host": "localhost:8099"},
        {"token": TOKEN},
        client_host="127.0.0.1",
        scheme="ws",
    )
    assert check_websocket(ws, TOKEN, DEFAULT_ALLOWED_ORIGINS) is True


def test_check_websocket_accepts_the_tailnet_origin_via_forwarded_headers():
    ws = _FakeWebSocket(
        {
            "origin": TAILNET,
            "host": "127.0.0.1:8099",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "kestrel-pi.tailnet-1234.ts.net",
        },
        {"token": TOKEN},
        client_host="127.0.0.1",
        scheme="ws",
    )
    assert check_websocket(ws, TOKEN, DEFAULT_ALLOWED_ORIGINS) is True


def test_check_websocket_ignores_forwarded_headers_from_a_non_loopback_peer():
    ws = _FakeWebSocket(
        {
            "origin": TAILNET,
            "host": "127.0.0.1:8099",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "kestrel-pi.tailnet-1234.ts.net",
        },
        {"token": TOKEN},
        client_host="198.51.100.9",
        scheme="ws",
    )
    assert check_websocket(ws, TOKEN, DEFAULT_ALLOWED_ORIGINS) is False


def test_end_to_end_a_direct_same_origin_pairing_post_is_not_403ed(tmp_path):
    """The orchestrator's exact repro: `uvicorn ... --host 127.0.0.1 --port
    8099`, browser opens http://localhost:8099/#pair=<token> directly, no
    tailscale/proxy anywhere. `/pair/validate` (a GET) worked before this fix
    - Origin isn't sent on a simple cross-origin-shaped GET the same way -
    but the pairing flow's `POST /clients/signals` (and everything else the
    app calls next) must not 403 either, with zero KESTREL_ALLOWED_ORIGINS
    configured."""
    config = Config(
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        memory_repo=tmp_path / "memory",
        identity_dir=tmp_path / "identity",
    )
    app = create_app(runtime=Runtime.build(config))
    client = TestClient(
        app,
        base_url=LOCAL_DIRECT,
        headers={"Authorization": f"Bearer {app.state.token}"},
        client=("127.0.0.1", 54321),
    )

    response = client.post(
        "/clients/signals", json={"app_open": True}, headers={"Origin": LOCAL_DIRECT}
    )
    assert response.status_code == 200


def test_end_to_end_the_configured_origin_gets_cors_and_passes_auth(tmp_path):
    """Same shape as test_cors.py, but with the origin coming from
    `KESTREL_ALLOWED_ORIGINS` rather than the built-in dev/Tauri set - the
    thing that actually has to work for the tailnet deployment."""
    config = Config(
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        memory_repo=tmp_path / "memory",
        identity_dir=tmp_path / "identity",
        allowed_origins=parse_allowed_origins(TAILNET),
    )
    app = create_app(runtime=Runtime.build(config))
    client = TestClient(app, headers={"Authorization": f"Bearer {app.state.token}"})

    allowed = client.get("/health", headers={"Origin": TAILNET})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == TAILNET

    refused = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert refused.status_code == 403
