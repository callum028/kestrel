"""Serving `web/dist` from the same server as the API - the PWA has to be
installable from the one tailnet URL `tailscale serve` fronts (docs/design.md
§11a), so there is no second static host to point it at. Three things this
suite pins down: the app shell loads without a token (a browser navigating
there for the first time cannot attach one), API routes are never shadowed
by the SPA catch-all, and the service worker is never cached.
"""

import pytest
from fastapi.testclient import TestClient

from kestrel.api import create_app
from kestrel.config import Config
from kestrel.runtime import Runtime


def _build_dist(dist_dir):
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<html><body>kestrel shell</body></html>")
    (dist_dir / "sw.js").write_text("// service worker")
    (dist_dir / "manifest.webmanifest").write_text('{"name": "Kestrel"}')
    assets = dist_dir / "assets"
    assets.mkdir()
    (assets / "app.abc123.js").write_text("console.log('hi')")


@pytest.fixture
def app(tmp_path):
    dist_dir = tmp_path / "dist"
    _build_dist(dist_dir)
    config = Config(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "kestrel.db",
        memory_repo=tmp_path / "data" / "memory",
        identity_dir=tmp_path / "data" / "identity",
        web_dist_dir=dist_dir,
    )
    return create_app(runtime=Runtime.build(config))


@pytest.fixture
def anon(app):
    """No Authorization header - what a browser's own page navigation sends."""
    return TestClient(app)


@pytest.fixture
def authed(app):
    return TestClient(app, headers={"Authorization": f"Bearer {app.state.token}"})


def test_root_serves_the_app_shell_without_a_token(anon):
    """The whole point: a phone opening the tailnet URL for the first time has
    no token yet and cannot attach an Authorization header to a navigation -
    if this needed one, pairing could never get off the ground."""
    response = anon.get("/")
    assert response.status_code == 200
    assert "kestrel shell" in response.text


def test_an_unknown_client_route_falls_back_to_the_shell(anon):
    """The SPA owns client-side routes - `/?delivery=<id>` (public/sw.js) and
    anything else the router invents must resolve to index.html, not a 404."""
    response = anon.get("/some/client/route")
    assert response.status_code == 200
    assert "kestrel shell" in response.text


def test_a_built_asset_is_served_verbatim(anon):
    response = anon.get("/assets/app.abc123.js")
    assert response.status_code == 200
    assert "console.log" in response.text


def test_sw_js_is_never_cached(anon):
    response = anon.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_manifest_is_served_without_a_token(anon):
    response = anon.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert "Kestrel" in response.text


def test_an_api_route_is_not_shadowed_by_the_spa_catch_all(authed):
    """The catch-all is mounted last precisely so this never happens - an
    unauthenticated call must still be refused, not quietly served the shell."""
    response = authed.get("/tasks")
    assert response.status_code == 200
    assert response.json() == []


def test_an_api_route_still_requires_a_token_even_with_static_serving_on(anon):
    response = anon.get("/tasks")
    assert response.status_code == 401
    assert response.json()["status"] == "refused"


def test_health_is_not_swallowed_by_the_spa_either(authed):
    response = authed.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_no_static_app_configured_means_api_only(tmp_path):
    """The default for a hand-built Config used throughout the rest of the
    suite - `web_dist_dir=None` - must keep behaving exactly as it did before
    this existed: no shell, root just isn't a route."""
    config = Config(
        data_dir=tmp_path,
        db_path=tmp_path / "kestrel.db",
        memory_repo=tmp_path / "memory",
        identity_dir=tmp_path / "identity",
    )
    app = create_app(runtime=Runtime.build(config))
    client = TestClient(app, headers={"Authorization": f"Bearer {app.state.token}"})
    response = client.get("/")
    assert response.status_code == 404
