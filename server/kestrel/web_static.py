"""Serving the built web app (`web/dist`) from the same origin as the API.

Why this exists at all: on the Pi, `tailscale serve` fronts exactly one
loopback port (docs/design.md §11a - binding wider than loopback plus relying
on the token alone would be a mistake). That means the phone and the desk both
reach Kestrel at one HTTPS tailnet URL, and the PWA has to be installable
*from* that URL - there is no second static host to point it at, and the old
Tauri shell (which used to serve the page locally and inject the token, see
`web/src-tauri`) is superseded (docs/design.md §13). So the API server is also
where the SPA comes from.

Three things this has to get right, in order of how easy they are to get
wrong:

1. **Never shadow an API route.** `create_app` mounts this last, after every
   `@app.get`/`@app.post`/`@app.websocket` above - Starlette matches routes in
   registration order, so `/tasks/{handle}` etc. are already spoken for by
   the time the catch-all below is added. A wildcard *first* would swallow
   every API call as "no matching static file, fall through to
   `index.html`", which is a silent 200 of the wrong page instead of a 404 -
   far worse than crashing, because it looks like success.
2. **SPA fallback for client-side routes.** `/?delivery=<id>` (a push
   notification deep link, see `public/sw.js`) and anything else the router
   invents client-side has to resolve to `index.html`, not a 404 - the
   client, not the server, owns those paths.
3. **`sw.js` is never cached.** A stale service worker is a worse failure
   mode than a slightly-stale asset: it can pin an old app shell in place
   indefinitely, immune to a normal reload. Vite's hashed asset filenames
   make aggressive caching safe for everything *except* the one file whose
   job is to notice there's a new version.

**A fourth thing, in `api.py` rather than here: the static app is reachable
without the bearer token.** Everything else in this system requires one on
every request (`auth.py`) - but a browser navigating to the page for the
first time cannot attach an `Authorization` header to that navigation, so if
loading `index.html` required the token, nothing could ever get far enough to
present it (see the pairing flow, `kestrel.pair` + `App.tsx`, whose entire
job is getting the token into that browser's `localStorage` *after* the page
has loaded). This is not a hole in the token model: the HTML/JS bundle is not
a secret, and `api.py`'s auth middleware decides the exemption by actually
matching the request against every registered API route first (a snapshot
taken before this module's catch-all is mounted) - a request only skips the
token check once every real API route, including the `GET` ones (`/health`,
`/tasks`, `/state`, ...), has already said no. The exemption can therefore
never widen by accident just because a path *looks* static.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

# Served with their real names at the root because the manifest, the SW
# registration (`navigator.serviceWorker.register("/sw.js")`, implied by
# scope "/" in the manifest) and Apple's PWA install heuristics all expect
# these exact paths - `/manifest.webmanifest` and `/sw.js` are not
# content-addressed like the rest of the build output.
_NO_CACHE_FILES = {"sw.js"}


def mount_web_app(app: FastAPI, dist_dir: Path) -> None:
    """Serve `dist_dir` (a `vite build` of `web/`) at `/`, with SPA fallback.

    Call this last, after every API route is registered - see the module
    docstring for why order matters here.
    """
    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        # Vite's own hashed-filename output - safe to cache forever, since a
        # changed file gets a changed name.
        app.mount("/assets", StaticFiles(directory=assets_dir), name="web-assets")

    icons_dir = dist_dir / "icons"
    if icons_dir.is_dir():
        app.mount("/icons", StaticFiles(directory=icons_dir), name="web-icons")

    def _file_response(path: Path) -> FileResponse:
        headers = {"Cache-Control": "no-cache"} if path.name in _NO_CACHE_FILES else None
        return FileResponse(path, headers=headers)

    @app.get("/{full_path:path}", name="web-spa")
    def spa(full_path: str) -> Response:
        # A path is served verbatim when the build produced a file at it
        # (`sw.js`, `manifest.webmanifest`, `favicon.ico`, anything under
        # `icons/`/`assets/` that a mount above didn't already catch) -
        # everything else is a client-side route, so it gets `index.html`
        # and the SPA's own router takes it from there.
        candidate = (dist_dir / full_path).resolve()
        if full_path and candidate.is_file() and dist_dir.resolve() in candidate.parents:
            return _file_response(candidate)
        return _file_response(dist_dir / "index.html")
