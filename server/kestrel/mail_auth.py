"""`python -m kestrel.mail_auth` - one-time device-code sign-in for mail reading.

Run once per mailbox, interactively, by Callum. It gets a refresh token scoped
to `Mail.Read offline_access` and writes it to disk via `TokenStore`
(`0600`, alongside the API token in `config.py`'s `token_path`); after that,
`GraphMailReader` refreshes access tokens on its own and this command is never
needed again unless the token is revoked in Entra.

Device code, not the auth-code + browser-redirect flow, because there is no
web server or redirect URI to receive a callback - Kestrel runs headless on a
Pi. The user visits a URL on any device, on their own already-authenticated
browser session, types a short code, and this process polls until they do.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

import httpx

from .config import Config
from .graph_mail import AUTHORITY, SCOPE, TokenStore


def run_device_code_flow(
    tenant_id: str,
    client_id: str,
    scope: str = SCOPE,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Blocks until the user completes sign-in, or the code expires. Returns
    the refresh token. Raises RuntimeError on anything else - declined
    consent, an unregistered app, a mistyped tenant.

    `sleep` is real `time.sleep` by default - overridden in tests exercising
    the `slow_down` backoff so they cover the retry logic without actually
    waiting out the (real, seconds-scale) polling interval."""
    http = client or httpx.Client()

    resp = http.post(
        f"{AUTHORITY}/{tenant_id}/oauth2/v2.0/devicecode",
        data={"client_id": client_id, "scope": scope},
    )
    resp.raise_for_status()
    device = resp.json()

    print(device["message"])  # e.g. "To sign in, use a web browser to open
    # https://microsoft.com/devicelogin and enter the code XXXXXXXX to authenticate."
    sys.stdout.flush()

    interval = device.get("interval", 5)
    deadline = time.monotonic() + device.get("expires_in", 900)

    while time.monotonic() < deadline:
        sleep(interval)
        resp = http.post(
            f"{AUTHORITY}/{tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device["device_code"],
            },
        )
        body = resp.json()
        if resp.status_code == 200:
            return body["refresh_token"]

        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise RuntimeError(f"device code sign-in failed: {error} - {body.get('error_description')}")

    raise RuntimeError("device code expired before sign-in completed - run this again")


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="kestrel.mail_auth", description=__doc__)
    parser.add_argument("--tenant-id", required=True, help="Entra tenant ID (or 'organizations')")
    parser.add_argument("--client-id", required=True, help="App registration's client ID")
    args = parser.parse_args(argv)

    config = Config.from_env()
    config.ensure_dirs()

    try:
        refresh_token = run_device_code_flow(args.tenant_id, args.client_id)
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"kestrel.mail_auth: {exc}", file=sys.stderr)
        sys.exit(1)

    TokenStore(config.mail_token_path).save(refresh_token)
    print(f"Saved. Mail reading is authorised; token stored at {config.mail_token_path}")
    print(
        f"Set KESTREL_MAIL_TENANT_ID={args.tenant_id} and "
        f"KESTREL_MAIL_CLIENT_ID={args.client_id} wherever Kestrel's environment is configured."
    )


if __name__ == "__main__":
    main()
