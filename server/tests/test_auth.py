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


class _FakeRequest:
    """Just enough of `fastapi.Request` for `check_request`."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = _Headers({k.lower(): v for k, v in headers.items()})


class _QueryParams(dict):
    pass


class _FakeWebSocket:
    """Just enough of `fastapi.WebSocket` for `check_websocket`."""

    def __init__(self, headers: dict[str, str], query: dict[str, str]) -> None:
        self.headers = _Headers({k.lower(): v for k, v in headers.items()})
        self.query_params = _QueryParams(query)


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
