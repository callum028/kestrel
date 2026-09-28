"""Authentication.

The security question for Kestrel is not subtle: `POST /terminals` spawns a
process in Callum's account, with his SSH keys, his GitHub token and his repos.
That is the feature, not a bug - which makes "who can reach the API" the whole
of the security model.

Two things are defended against, and the second is the one people miss:

1. **Anything on the network.** Bind to loopback, and require a token.
2. **Any web page he has open.** A browser will happily let evil.example.com
   issue requests to http://localhost:8099. Loopback is not a boundary for a
   browser. This is how a string of local dev tools have been compromised.

The token is a header, deliberately never a cookie: browsers attach cookies
automatically to cross-site requests, which is exactly the hole. Origin is
checked too, so a page that guesses the token still cannot use it from a tab.

WebSockets take the token as a query parameter, because the browser WebSocket
API cannot set headers. That leaks it into logs, so the token is treated as a
local secret rather than a bearer credential worth stealing at scale.
"""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from fastapi import HTTPException, Request, WebSocket, status

# Where a legitimate client can be running. Anything else is a page that found
# its way to the port. This is the dev/Tauri set; a deployment reachable at a
# tailnet URL (via `tailscale serve`) adds to it through
# `KESTREL_ALLOWED_ORIGINS` (see `parse_allowed_origins` and
# `config.Config.allowed_origins`) rather than editing this file - the rule
# itself (unrecognised origin -> refused) never changes.
DEFAULT_ALLOWED_ORIGINS = frozenset(
    {
        "http://localhost:5173",  # vite dev
        "http://127.0.0.1:5173",
        "http://localhost:1420",  # tauri dev
        "tauri://localhost",  # tauri production (windows/linux)
        "http://tauri.localhost",  # tauri production (windows webview2)
    }
)


def parse_allowed_origins(extra: str | None) -> frozenset[str]:
    """`KESTREL_ALLOWED_ORIGINS` is a comma-separated list of additional
    origins - typically the one `https://<host>.<tailnet>.ts.net` URL
    `tailscale serve` fronts the app at. Always additive to the dev/Tauri
    defaults, never a replacement: there is no way to configure this that
    reopens loopback dev access by accident."""
    extra_origins = {o.strip() for o in (extra or "").split(",") if o.strip()}
    return DEFAULT_ALLOWED_ORIGINS | frozenset(extra_origins)


def load_or_create_token(path: Path) -> str:
    """One token per install, on disk, readable only by the owner."""
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)
    path.chmod(0o600)
    return token


def _matches(supplied: str | None, expected: str) -> bool:
    # Constant time, so a wrong token cannot be narrowed down by timing it.
    return supplied is not None and hmac.compare_digest(supplied, expected)


def check_request(
    request: Request, token: str, origins: frozenset[str] = DEFAULT_ALLOWED_ORIGINS
) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin not in origins:
        # A same-origin or non-browser caller sends no Origin at all; a browser
        # always does. So an unrecognised one is a page, not a client.
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"origin not allowed: {origin}")

    header = request.headers.get("authorization", "")
    supplied = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else None
    if not _matches(supplied, token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing token")


def check_websocket(
    websocket: WebSocket, token: str, origins: frozenset[str] = DEFAULT_ALLOWED_ORIGINS
) -> bool:
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in origins:
        return False
    return _matches(websocket.query_params.get("token"), token)
