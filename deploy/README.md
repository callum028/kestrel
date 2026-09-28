# Running Kestrel on the Pi

Short runbook. For the full design, see `../docs/design.md` (§10 Infrastructure,
§11 Failure modes, §11a Security). For a from-scratch rebuild, see
`restore.md`.

## Topology

Two systemd **user** units (not system units - see the comments in
`systemd/kestrel-*.service` for why: unprivileged account, no sudo needed to
manage them day to day):

- `kestrel-session-host.service` - owns every Claude Code PTY. Long-lived.
  Restarting the server never touches this.
- `kestrel-server.service` - FastAPI/uvicorn, bound to `127.0.0.1:8099` only.
  This is what gets redeployed often.

Plus `kestrel-backup.timer` / `.service` (nightly SQLite + memory repo
backup).

`~/kestrel/current` is a symlink to the release currently live
(`~/kestrel/releases/<tag>/`), each with its own venv. Both units execute
through that symlink, so a deploy is a symlink flip + restart, and a rollback
is the same in reverse.

## First install on a new Pi

```sh
git clone <repo-url> ~/kestrel-bootstrap
cd ~/kestrel-bootstrap
KESTREL_REPO_URL=<repo-url> ./deploy/pi-setup.sh
```

Follow the manual steps it prints (Tailscale login, Claude Code login, `gh
auth login`, writing the real `kestrel.env`), then:

```sh
~/kestrel/src/deploy/deploy.sh <tag>
systemctl --user enable --now kestrel-session-host.service
systemctl --user enable --now kestrel-backup.timer
sudo tailscale serve --bg 127.0.0.1:8099
```

## Deploying a new version

```sh
~/kestrel/src/deploy/deploy.sh v1.4.0
```

This fetches, checks out the tag into `~/kestrel/releases/v1.4.0/` (a `git
worktree`, so it's fast even on a Pi), builds that release's own venv,
flips `current`, restarts **only** `kestrel-server.service`, and polls
`/health`. If the health check fails, it automatically flips `current` back
and restarts the server on the previous release - you land back where you
started, not on a broken deploy.

The session host is never restarted by a normal deploy. If a change actually
requires it (a change to `agent/`), say so explicitly:

```sh
~/kestrel/src/deploy/deploy.sh v1.4.0 --restart-host
```

This prompts for confirmation (`--yes` to skip, e.g. from CI) because **it
kills every running Claude Code session** - there is no undo for work in
flight.

## Rolling back manually

`deploy.sh` rolls back automatically on a failed health check. To roll back
by hand (e.g. the health check passed but something's still wrong):

```sh
~/kestrel/src/deploy/deploy.sh <previous-known-good-tag>
```

Re-running deploy.sh with an older tag is the rollback mechanism - it's
idempotent, so pointing back at a tag whose release dir already exists just
reuses it (rebuilding the venv if needed) and flips the symlink.

## Checking status / logs

```sh
systemctl --user status kestrel-server.service kestrel-session-host.service
journalctl --user -u kestrel-server -f          # tail server logs
journalctl --user -u kestrel-session-host -f    # tail session host logs
curl -H "Authorization: Bearer $(cat ~/.kestrel/token)" http://127.0.0.1:8099/health
```

`/health` (see `server/kestrel/api.py`) returns `active_tasks`, whether the
dev-environment lock is held, and pending deliveries - a quick sanity check
beyond "the process is up."

## Backups

Nightly via `kestrel-backup.timer` (03:15 + jitter). Runs `backup.sh`:
SQLite online backup (`sqlite3 .backup`, safe against the live WAL database -
never a raw file copy, see the comments in `backup.sh` for why that matters),
pushed to `BACKUP_REMOTE`; the memory repo (already committed locally on
every write by Kestrel itself) pushed to `MEMORY_BACKUP_REMOTE` (or
`BACKUP_REMOTE` if that's unset). The secrets file is never touched by this
script under any circumstance.

Run it by hand before something risky:

```sh
systemctl --user start kestrel-backup.service
journalctl --user -u kestrel-backup -n 50
```

Restoring from a backup: see `restore.md`.

## Tailscale Serve (HTTPS on the tailnet)

The server binds loopback only (`127.0.0.1:8099` - see `systemd/kestrel-server.service`
and `docs/design.md` §11a: a browser can hit any port on `localhost` from any
open tab, so binding wider than loopback plus relying on the token alone
would be a mistake). `tailscale serve` terminates TLS and proxies from the
tailnet to that loopback port:

```sh
sudo tailscale serve --bg 127.0.0.1:8099
tailscale serve status
```

This is **tailnet-only** - reachable solely from devices in your tailnet, not
the public internet. **Never use `tailscale funnel` for this.** Funnel is
public internet exposure; the only endpoint in this system ever meant for
that is `/hooks/github` (GitHub webhook receiver, HMAC-verified), and it is
not part of this step.

To stop serving: `sudo tailscale serve --https=443 off` (see `tailscale serve
status` for the exact port/path it's currently bound to).

## "Can't reach Kestrel"

What a client should show when it can't reach the API, and what it usually
means:

| Symptom | Likely cause |
|---|---|
| Connection refused / timeout over the tailnet | Pi is off, or Tailscale is down on the Pi or the client. Check `tailscale status` on both ends. |
| TLS error on the tailnet URL | `tailscale serve` isn't running / was reconfigured. `sudo tailscale serve status` on the Pi. |
| 401 from `/health` or anything else | Token mismatch - client has a stale token from before a restore/rotation. Re-read `~/.kestrel/token` into the client. |
| Reachable, `/health` returns `dev_lock` non-null indefinitely, tasks not progressing | Server is up but something's stuck - check `journalctl --user -u kestrel-server`, not a connectivity problem. |
| `/terminals` endpoints report `"status": "unavailable"` | Server is up but the session host isn't. `systemctl --user status kestrel-session-host.service`. The server itself is fine - this is the degraded mode it's designed to report rather than crash into. |

Clients should show "can't reach Kestrel" plainly rather than a spinner or a
stale-looking UI (docs/design.md §11) - if you're building a client and it
isn't doing this, that's a bug in the client, not something to fix here.
