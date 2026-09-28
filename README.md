# Kestrel

An always-on personal assistant built around a capability host. Its first capability is supervising
Claude Code; its defining property is that it exists when you are not at the keyboard.

Not a coding agent — Claude Code is. Not a voice assistant — voice is one output channel. Not a
notifier — alerting is what it does when it has failed to deliver.

**[docs/design.md](docs/design.md) is the canonical design.** It holds decisions, contracts, and
boundaries. Implementation detail lives in code, not there.

## Layout

| | |
|---|---|
| `server/` | The Kestrel service. Runs on the always-on host (Pi). Owns all durable state. |
| `agent/` | The session host and executor. Owns every PTY (`python -m kestrel_agent`), long-lived and independent of the server — git worktrees, Claude Code sessions, validation. |
| `host/` | Small Windows process. Owns the tailnet link, tray, notifications, window focus, idle time. |
| `web/` | Desktop and phone UI (responsive, installable as a PWA). Chat, task panes, embedded terminal, web push. |
| `android/` | Native phone client (not yet built - `web/`'s PWA covers phone use for now). Activation, calls, notifications. |
| `docs/` | Design. |

## Development

Two checkouts of this repo:

- **WSL** (`~/workspace/github/kestrel`) — `server/`, `agent/`. Must live on the Linux filesystem;
  worktree-per-task over `/mnt/c` is slow.
- **Windows** — anything that must build natively.

Pi access: `ssh callum028@kestrel-pi` (key lives in WSL, not Windows).

For local development, start the same two processes described in [Running v1](#running-v1) below,
with `--reload` on the uvicorn command. They talk over a Unix domain socket at
`$KESTREL_DATA/session-host.sock` (`~/.kestrel` by default), `0600`, created by the session host.
Restarting or redeploying the server never touches this socket or the processes behind it — that is
the point of the split. If the server can't reach the socket, its `/terminals` endpoints report
`"status": "unavailable"` rather than an empty list.

## Running v1

Two long-lived processes, on the Pi in production or locally for development — started separately,
in either order (the server is a client of the session host, never its parent):

```sh
python -m kestrel_agent            # the session host: owns every PTY, start it first and leave it running
uvicorn kestrel.api:create_app --factory --reload   # the server: everything else
```

**Config:** one env file, `deploy/kestrel.env.example` — copy it, fill in what you need, `chmod 600`
it. Every capability degrades gracefully when its section is left unset (fake board, fake mail
reader, no brain, no GitHub landing/validation) rather than failing to start — see the file itself
for exactly which variables each thing reads and what happens without them.

**Enabling the brain:** set `KESTREL_BRAIN_CLAUDE_BINARY` (typically `claude`) — that alone turns it
on; without it, Kestrel still runs with the stub conversational responder and every delivery falls
back to its fixed deterministic template instead of being reworded. See `kestrel.env.example`'s
"Brain" section for the MCP command override most deployments won't need.

**First run on the Pi:**

1. `deploy/pi-setup.sh` — installs everything unattended-safe (uv, Node, the Claude Code CLI,
   Playwright, `gh`, Tailscale, systemd user units) and prints the manual steps below.
2. Blocking, interactive steps `pi-setup.sh` cannot do for you: `sudo tailscale up` (opens a URL to
   approve), `claude` (logs in via a browser on another device), and `gh auth login` if sessions will
   open PRs via `gh` under this account.
3. Copy and fill in the env file (above), `chmod 600` it.
4. `deploy/deploy.sh <tag>` — first real deploy: builds the release venv **and the web app**
   (`web/dist`, served by the server itself — see `server/kestrel/web_static.py`), points `current`
   at it, starts `kestrel-server.service`, health-checks, rolls back automatically on failure. Then
   start the session host once (`deploy.sh` never touches it): `systemctl --user enable --now
   kestrel-session-host.service`.
5. `systemctl --user enable --now kestrel-backup.timer` for nightly backups.
6. `sudo tailscale serve --bg 127.0.0.1:8099` so the web app is reachable over tailnet HTTPS (never
   Funnel — see `deploy/README.md`). Add that URL to `KESTREL_ALLOWED_ORIGINS` in the env file and
   restart the server, or every request from it 403s (`docs/design.md` §11a).
7. Pair each device: `kestrel-pair --base https://<your-tailnet-url>` prints a one-time link — the
   token rides in the URL fragment, never baked into the bundle and never logged. Open it once on
   each phone/desktop to store the token there, then install the page as a PWA. See
   `deploy/README.md`'s "Tailscale Serve" section for the full walkthrough and how to un-pair a
   device later.

### Mail (Microsoft Graph)

Read-only, read on request only — see docs/design.md §4.4/§4.5 and §11 for why. One-time setup:

1. In Entra ID: **App registrations → New registration** — single tenant, no redirect URI needed.
2. **Authentication → Advanced settings → Allow public client flows** = Yes (device code needs a
   public client; there is deliberately no client secret to leak).
3. **API permissions → Add a permission → Microsoft Graph → Delegated** → add `Mail.Read` and
   `offline_access` → **Grant admin consent** (Callum is effectively the tenant admin here).
4. Copy the **Directory (tenant) ID** and **Application (client) ID** from the registration's
   Overview page.
5. Set `KESTREL_MAIL_TENANT_ID` and `KESTREL_MAIL_CLIENT_ID` in the environment, then run:
   ```sh
   kestrel-mail-auth --tenant-id <tenant> --client-id <client>
   ```
   This opens a device-code sign-in (visit a URL, type a short code) and stores a refresh token at
   `$KESTREL_DATA/mail_token`, `0600`. `GraphMailReader` refreshes access tokens on its own after
   that — this command is only needed again if the token is revoked.

Without those two environment variables and a stored token, `Runtime` falls back to
`kestrel.mail.FakeMailReader` and logs a warning saying so — Kestrel still starts, just against
placeholder mail.

## Status

Built and tested (agent + server unit/integration suites, plus one end-to-end test driving the real
app + a real session host + fake `claude` binaries through a full task lifecycle): the server core
(tasks, attention, memory, events, delivery/narration), the session host, the Claude Code executor
(worktree-per-task PTY sessions, hooks, claim validation), Kestrel-owned waits, GitHub PR
landing/merge-on-green and Kestrel's own post-merge validation, Notion board sync, read-only mail,
the brain (headless `claude -p`, `kestrel-mcp`, async conversation, narration), the web terminal, and
the responsive desktop/phone web UI (chat, task panes, push notifications).

Still to build: a native Android client (`android/`, currently covered by the responsive web UI as a
PWA), the Windows tray/notification host (`host/`), voice, and webhook-driven Notion/GitHub sync
(both currently poll). See §12 of the design for the spikes to run first; the two phone ones need a
physical device.
