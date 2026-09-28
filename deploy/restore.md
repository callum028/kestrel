# Restoring Kestrel onto a new Pi

Target: dead Pi to working Kestrel inside about an hour, per docs/design.md
§11 ("Pi down" is the top failure mode - there's no HA for one user, so
recovery speed is the mitigation).

Have ready before you start: the new Pi flashed with Raspberry Pi OS
(bookworm, 64-bit), booted, SSH reachable; the `kestrel.env` secrets from your
password manager; the `BACKUP_REMOTE` / `MEMORY_BACKUP_REMOTE` git remote(s)
this Pi was pushing to.

## 1. Base OS (~10 min)

Flash the SD/SSD, boot, `sudo raspi-config` to set hostname/locale if you
care, `sudo apt-get update && sudo apt-get upgrade -y`. If booting from SSD,
follow the standard Raspberry Pi Imager / `rpi-eeprom` boot-order steps
before continuing - out of scope for this doc.

## 2. Bootstrap (~10-15 min)

```sh
git clone <repo-url> ~/kestrel-bootstrap   # anywhere temporary is fine
cd ~/kestrel-bootstrap
KESTREL_REPO_URL=<repo-url> ./deploy/pi-setup.sh
```

Then do the manual steps it prints:

```sh
sudo tailscale up          # approve the device in the admin console
claude                     # log in with Callum's Max account (browser prompt)
gh auth login              # only if sessions manage PRs via gh
```

## 3. Secrets (~2 min)

Pull `kestrel.env` out of the password manager (never out of a backup - it is
deliberately never included in one, see deploy/backup.sh) and place it:

```sh
mkdir -p ~/.config/kestrel && chmod 700 ~/.config/kestrel
$EDITOR ~/.config/kestrel/kestrel.env   # paste the real values
chmod 600 ~/.config/kestrel/kestrel.env
```

## 4. Restore data (~5-10 min)

`pi-setup.sh` already created `~/.kestrel` (mode 0700). Restore into it
*before* the server or session host has ever started, so nothing races a
fresh empty DB into existence first:

```sh
KESTREL_DATA="$HOME/.kestrel"

# SQLite: clone the backup repo and copy the latest snapshot in.
git clone "$BACKUP_REMOTE" /tmp/kestrel-backup-restore
cp /tmp/kestrel-backup-restore/kestrel.db "$KESTREL_DATA/kestrel.db"

# Memory repo: clone straight into place (it's a git repo already).
git clone "${MEMORY_BACKUP_REMOTE:-$BACKUP_REMOTE}" "$KESTREL_DATA/memory"
# If MEMORY_BACKUP_REMOTE shares one repo with the SQLite backup on a
# different branch, `git clone -b <branch>` that branch instead.

rm -rf /tmp/kestrel-backup-restore
```

Verify the DB isn't corrupt before trusting it:

```sh
sqlite3 "$KESTREL_DATA/kestrel.db" "PRAGMA integrity_check;"
```

Anything other than a single line reading `ok` means don't proceed on this
snapshot - go one commit further back in the backup repo's history
(`git log`, `git checkout <older-commit> -- kestrel.db`) and retry.

The auth token (`$KESTREL_DATA/token`) is deliberately NOT backed up - it's
regenerated fresh on first server start (`server/kestrel/auth.py:
load_or_create_token`). Every client (desktop, phone) will need the new token
after a restore; that's expected, not a bug.

## 5. Deploy (~5-10 min)

```sh
~/kestrel/src/deploy/deploy.sh <last-known-good-tag>
systemctl --user enable --now kestrel-session-host.service
systemctl --user enable --now kestrel-backup.timer
```

`deploy.sh` health-checks `/health` itself; if it reports success, the server
is up and reading the restored database.

## 6. Tailscale Serve (~2 min)

```sh
sudo tailscale serve --bg 127.0.0.1:8099
tailscale serve status   # confirm it's up
```

Note the new tailnet hostname/URL if it changed, and update any client
bookmarks / PWA install pointing at the old one.

## 7. Verify

- `curl -H "Authorization: Bearer $(cat ~/.kestrel/token)" https://<tailnet-host>/health` returns `{"ok": true, ...}`.
- `systemctl --user status kestrel-session-host.service kestrel-server.service` both `active (running)`.
- Open the web app, confirm task history and recent events are present (proves the SQLite restore actually took).
- Start a trivial terminal session, confirm the session host is actually spawning PTYs.

## What is NOT restored, and why

- **The auth token** - regenerated on purpose (§11a: it's a local secret, not
  worth trying to preserve across a full rebuild).
- **The secrets file** - never backed up; comes from the password manager
  every time (see deploy/backup.sh's comments).
- **In-flight terminal state** - a dead Pi's PTYs are gone; the event log +
  worktrees on the old disk (if recoverable at all) are the only record. This
  restore procedure assumes the old disk is not recoverable.
