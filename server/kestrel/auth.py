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

Since the web app itself is served from this same process (`web_static.py`),
an allowed origin also includes "the origin the browser was actually served
this app from" - a same-origin POST or websocket upgrade carries an `Origin`
header regardless of the allowlist, and refusing it would break the app that
is supposed to be talking to this server. See `_effective_server_origin` for
exactly what "same-origin" means once `tailscale serve` is proxying.
"""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path
from typing import Any

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
    reopens loopback dev access by accident. Additive also to same-origin
    (see `_effective_server_origin`), which needs no configuration at all -
    this exists for origins the server cannot recognise as itself, e.g. a
    reverse proxy on a different host entirely."""
    extra_origins = {o.strip() for o in (extra or "").split(",") if o.strip()}
    return DEFAULT_ALLOWED_ORIGINS | frozenset(extra_origins)


# The only peers that can possibly be legitimate sources of forwarded-header
# information - see `_effective_server_origin`.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def _effective_server_origin(
    headers: Any, client_host: str | None, scope_scheme: str
) -> str | None:
    """What the server believes its own origin is, for deciding whether an
    `Origin` header naming exactly that is telling the truth rather than
    being an allowlisted origin.

    Two cases, both real:

    1. **A direct connection - no proxy at all.** A browser at
       `http://localhost:8099` (the plain dev/loopback address, with nothing
       in front of it) sends `Origin: http://localhost:8099` on every
       same-origin POST/websocket-upgrade (browsers do this regardless of
       same-origin-ness for state-changing requests - this is not a Tauri or
       tailnet-specific quirk). `Host` plus the scheme the ASGI server
       actually accepted the connection on (`scope_scheme` - always `http`/
       `ws` here, since this process is never given TLS certs of its own)
       is already the truth for this case, no header trust decision needed.
    2. **Behind `tailscale serve`.** The public URL is
       `https://<host>.<tailnet>.ts.net`, TLS-terminated by `tailscale
       serve` and forwarded as plain HTTP to this loopback port - so `Host`
       alone is not reliable (a reverse proxy is free to rewrite it to the
       backend's own address) and the real scheme is `https`, not what this
       process itself sees. `X-Forwarded-Host`/`X-Forwarded-Proto` carry
       that - but only trusted when `client_host` (the actual TCP peer, not
       anything a request can claim) is loopback. This process binds
       loopback only (docs/design.md §11a), so the one legitimate source of
       a proxied request is something already running on this same
       machine; a remote caller cannot open a TCP connection here at all,
       let alone set headers "from" one that could.

    Returns `None` when there is no `Host` header to reason about at all
    (a non-browser caller with no Origin either, which is a separate,
    already-handled case in `check_request`/`check_websocket`).
    """
    host = headers.get("host")
    scheme = scope_scheme
    if client_host in _LOOPBACK_HOSTS:
        host = headers.get("x-forwarded-host", host)
        scheme = headers.get("x-forwarded-proto", scheme)
    if not host:
        return None
    scheme = "https" if scheme in ("https", "wss") else "http"
    return f"{scheme}://{host}"


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
        # Most same-origin requests send no Origin at all, but a browser does
        # send one on same-origin POSTs and websocket upgrades regardless -
        # that is not a page that found its way here, it is the app talking
        # to the server it was served from. See `_effective_server_origin`.
        client_host = request.client.host if request.client else None
        server_origin = _effective_server_origin(request.headers, client_host, request.url.scheme)
        if origin != server_origin:
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
        client_host = websocket.client.host if websocket.client else None
        server_origin = _effective_server_origin(
            websocket.headers, client_host, websocket.url.scheme
        )
        if origin != server_origin:
            return False
    return _matches(websocket.query_params.get("token"), token)
