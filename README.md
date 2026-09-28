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
| `web/` | Desktop UI. Chat, task panes, embedded terminal. |
| `android/` | Phone client. Activation, calls, notifications. |
| `docs/` | Design. |

## Development

Two checkouts of this repo:

- **WSL** (`~/workspace/github/kestrel`) — `server/`, `agent/`. Must live on the Linux filesystem;
  worktree-per-task over `/mnt/c` is slow.
- **Windows** — anything that must build natively.

Pi access: `ssh callum028@kestrel-pi` (key lives in WSL, not Windows).

### Running it

Two processes, started separately, in either order — the server is a client of the session host,
never its parent:

```sh
python -m kestrel_agent            # the session host: owns every PTY, start it first and leave it running
uvicorn kestrel.api:create_app --factory --reload   # the server: proxies terminal calls over a Unix socket
```

They talk over a Unix domain socket at `$KESTREL_DATA/session-host.sock` (`~/.kestrel` by default),
`0600`, created by the session host. Restarting or redeploying the server never touches this socket or
the processes behind it — that is the point of the split. If the server can't reach the socket, its
`/terminals` endpoints report `"status": "unavailable"` rather than an empty list.

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
   python -m kestrel.mail_auth --tenant-id <tenant> --client-id <client>
   ```
   This opens a device-code sign-in (visit a URL, type a short code) and stores a refresh token at
   `$KESTREL_DATA/mail_token`, `0600`. `GraphMailReader` refreshes access tokens on its own after
   that — this command is only needed again if the token is revoked.

Without those two environment variables and a stored token, `Runtime` falls back to
`kestrel.mail.FakeMailReader` and logs a warning saying so — Kestrel still starts, just against
placeholder mail.

## Status

Core server (tasks, attention, memory, events), the session host, and the web terminal exist and are
tested. Everything else in the design — executors beyond Claude Code, Notion/GitHub integration, the
phone client, voice — is still to build. See §12 of the design for the spikes to run first; the two
phone ones need a physical device.
