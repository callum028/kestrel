"""`kestrel-pair` / `python -m kestrel.pair` - get the API token onto a device
without baking it into the bundle and without cookies.

docs/design.md §11a is explicit that the token is a header, never a cookie,
and (§13) the old Tauri shell that used to inject it at startup is gone - a
phone's browser has neither mechanism. What it *can* do is follow a link. So
pairing is: print a URL whose fragment carries the token, open it once on the
device, and the web app (`web/src/App.tsx`'s pairing flow) takes it from
there - validates it against the server, stores it in `localStorage`, and
scrubs the fragment out of the address bar.

**Why a fragment and not a query parameter.** Everything after `#` is
resolved client-side only - it is never sent in the HTTP request, so it never
reaches the server's access log, `tailscale serve`'s proxy log, or any CDN or
history in between. A query parameter would end up in all three. This is the
same reasoning `terminalSocket()` in `web/src/api.ts` explicitly does *not*
get to use for the WebSocket token (the browser WebSocket API cannot set
headers, so that one really does have to ride in the URL and be treated as
already-somewhat-exposed) - a pairing link has no such constraint, so it gets
the safer of the two.

**Why no QR library.** A one-time pairing happens on a device that already has
a browser open (it's *becoming* a Kestrel client), which means the link can
travel there by whatever's easiest - paste it, AirDrop it, text it to
yourself, or scan a QR code if one's convenient. Pulling in a QR-rendering
dependency (`qrcode`, plus a rendering backend) for a codepath used a handful
of times per install is not worth a new pip dependency Kestrel's venv doesn't
otherwise need. Instead: if the system `qrencode` binary happens to be on
`PATH` (a single small package, not a pip dependency of this project, and not
required), this prints an ANSI QR code as a bonus; if it isn't, the link
alone is printed, which is sufficient on its own.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from .auth import load_or_create_token
from .config import Config


def pairing_url(base_url: str, token: str) -> str:
    """`base_url` is the tailnet HTTPS URL `tailscale serve` fronts the app
    at (e.g. `https://kestrel-pi.tailXXXX.ts.net`) - the same origin the web
    app is served from once `web_static.py` is wired up, so the pairing
    fragment never has to cross an origin boundary to matter. The token
    lands after `#`, deliberately - see the module docstring."""
    return f"{base_url.rstrip('/')}/#pair={token}"


def print_qr(url: str, *, out=sys.stdout) -> bool:
    """Best-effort ANSI QR code via the system `qrencode` binary, if present.
    Returns whether it printed one - `main()` uses this only to decide
    whether to also print a one-line note that a QR wasn't available."""
    qrencode = shutil.which("qrencode")
    if qrencode is None:
        return False
    try:
        subprocess.run([qrencode, "-t", "ANSIUTF8", url], check=True, stdout=out)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def main(argv: list[str] | None = None) -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="kestrel.pair", description=__doc__)
    parser.add_argument(
        "--base",
        default=os.environ.get("KESTREL_PUBLIC_URL"),
        help=(
            "The tailnet HTTPS URL the app is reachable at, e.g. "
            "https://kestrel-pi.tailXXXX.ts.net (the address `tailscale serve` fronts, "
            "not KESTREL_SERVER_URL - that one is loopback-only and useless to a phone). "
            "Defaults to $KESTREL_PUBLIC_URL."
        ),
    )
    parser.add_argument(
        "--no-qr",
        action="store_true",
        help="Skip the terminal QR code even if `qrencode` is on PATH.",
    )
    args = parser.parse_args(argv)

    if not args.base:
        print(
            "kestrel.pair: no base URL given - pass --base or set KESTREL_PUBLIC_URL "
            "to the tailnet HTTPS address (see deploy/README.md's tailscale serve section)",
            file=sys.stderr,
        )
        sys.exit(1)

    config = Config.from_env()
    config.ensure_dirs()
    token = load_or_create_token(config.token_path)
    url = pairing_url(args.base, token)

    print("Pairing link - the token rides in the URL fragment, never sent to the")
    print("server or logged anywhere. Open it once on the device you're pairing:")
    print()
    print(f"  {url}")
    print()

    if not args.no_qr and not print_qr(url):
        print("(install `qrencode` to also get a scannable QR code here - optional)")
        print()

    print("The web app reads the fragment on load, checks it against the server,")
    print("stores it in that browser's localStorage, and strips it from the address")
    print("bar. To un-pair a device later, use the 'unpair' action in its app menu -")
    print("this command only ever adds a device, it does not revoke anything.")


if __name__ == "__main__":
    main()
