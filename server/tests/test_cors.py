"""Cross-origin access for the desktop app.

The desktop shell is served from tauri://localhost and talks to the API on
another origin, so every call is cross-origin. The first build of the app
connected to nothing at all because of exactly this class of problem, so the
preflight path is worth pinning down.
"""

import pytest
from fastapi.testclient import TestClient

from kestrel.api import create_app
from kestrel.config import Config
from kestrel.runtime import Runtime

TAURI = "http://tauri.localhost"


@pytest.fixture
def app(tmp_path):
    return create_app(
        runtime=Runtime.build(
            Config(
                data_dir=tmp_path,
                db_path=tmp_path / "kestrel.db",
                memory_repo=tmp_path / "memory",
                identity_dir=tmp_path / "identity",
            )
        )
    )


@pytest.fixture
def client(app):
    return TestClient(app, headers={"Authorization": f"Bearer {app.state.token}"})


def test_a_preflight_is_answered_without_a_token(app):
    """A preflight carries no Authorization header by definition. Rejecting it
    would block the app before it ever got the chance to authenticate."""
    anonymous = TestClient(app)
    response = anonymous.options(
        "/tasks",
        headers={
            "Origin": TAURI,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == TAURI
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_the_desktop_origin_gets_cors_headers_on_a_real_call(client):
    response = client.get("/health", headers={"Origin": TAURI})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == TAURI


def test_an_unknown_origin_is_not_given_cors_headers(client):
    response = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in response.headers
    # and the origin check refuses it outright
    assert response.status_code == 403


def test_preflight_from_an_unknown_origin_is_not_approved(app):
    anonymous = TestClient(app)
    response = anonymous.options(
        "/tasks",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in response.headers


def test_credentials_are_never_allowed(client):
    """No cookies anywhere, deliberately: the token is a header precisely so a
    browser cannot attach it automatically to a cross-site request."""
    response = client.get("/health", headers={"Origin": TAURI})
    assert response.headers.get("access-control-allow-credentials") != "true"
