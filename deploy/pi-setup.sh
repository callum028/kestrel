#!/usr/bin/env bash
# Bootstrap a Raspberry Pi 4B (8GB, 64-bit Raspberry Pi OS / Debian bookworm,
# arm64) to run Kestrel. Idempotent: re-running after a partial run, or
# months later to pick up a package bump, is safe - every step checks
# whether it already did its job before doing it again.
#
# What this script does NOT do, on purpose (interactive auth / one-time
# secrets - printed as manual steps at the end):
#   - `tailscale up` (needs your account, a browser or auth key)
#   - `claude` login (needs your Max account, opens a browser)
#   - writing the real kestrel.env (secrets don't belong in a script's argv
#     or a shell history)
#   - `gh auth login` (only needed if Claude Code sessions push/open PRs
#     with the gh CLI under this account, rather than a git remote + token)
#
# Sources for install commands (checked, not guessed, 2026-09-28 - re-check
# if this script is more than a few months old, install methods for all
# three of these have moved before):
#   - uv:        https://docs.astral.sh/uv/getting-started/installation/
#   - Tailscale: https://tailscale.com/kb/1031/install-linux
#   - Claude Code: https://code.claude.com/docs/en/setup (native installer -
#     no Node dependency; it ships a standalone binary and auto-updates
#     itself in the background)
set -euo pipefail

KESTREL_USER="${KESTREL_USER:-$(whoami)}"
KESTREL_DATA="${KESTREL_DATA:-$HOME/.kestrel}"
CONFIG_DIR="$HOME/.config/kestrel"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
NODE_MAJOR=22  # Claude Code's npm package needs Node 22+; the native
               # installer below doesn't need Node at all, but Playwright
               # (npx) does, so this is installed regardless.

log() { printf '\n=== %s ===\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

if [ "$(id -u)" = "0" ]; then
    echo "Run this as the unprivileged Pi user (the one the systemd --user" >&2
    echo "units and Kestrel itself will run as), not as root. It uses sudo" >&2
    echo "internally for the handful of steps that need it." >&2
    exit 1
fi

# --- 1. System packages ------------------------------------------------------
log "apt packages"
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential \
    sqlite3 \
    gpg \
    jq

# --- 2. uv (Python package/venv manager) + Python 3.12 ----------------------
log "uv"
if ! have uv; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer places uv in ~/.local/bin, which a fresh non-login shell
    # may not have on PATH yet in this same script run.
    export PATH="$HOME/.local/bin:$PATH"
fi
uv python install 3.12

# --- 3. Node.js (for Claude Code's own tool use, and for Playwright/npx) ----
log "Node.js ${NODE_MAJOR}.x"
if ! have node || [ "$(node -v | sed -E 's/^v([0-9]+).*/\1/')" -lt "$NODE_MAJOR" ]; then
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | sudo -E bash -
    sudo apt-get install -y nodejs
fi

# --- 4. Claude Code CLI -------------------------------------------------------
# Native installer: standalone binary, no Node dependency, auto-updates
# itself in the background. Login (`claude`, follow the browser prompt
# against Callum's Max account) is a manual step - it opens a browser and
# there's no headless/token flow worth scripting for a single-user box.
log "Claude Code CLI"
if ! have claude; then
    curl -fsSL https://claude.ai/install.sh | bash
    export PATH="$HOME/.local/bin:$PATH"
fi

# --- 5. Playwright + Chromium -------------------------------------------------
# No project here pins a Playwright version yet (product context: "Playwright
# (Chromium) runs on the Pi against the online dev server" - not wired into
# any package.json today). Installed globally via npm so it's available to
# whatever eventually shells out to it; pin a version in that code's own
# package.json once it exists and drop this global install.
log "Playwright + Chromium"
sudo npm install -g playwright
# --with-deps pulls the apt packages Chromium needs (fonts, codecs, etc.) -
# requires sudo on Debian-family systems, which the playwright CLI invokes
# itself.
sudo PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers npx -y playwright install --with-deps chromium
echo "export PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers" | \
    sudo tee /etc/profile.d/playwright-browsers.sh >/dev/null
# Also drop it in kestrel.env so it's visible to whatever process launches
# Playwright, without relying on /etc/profile.d being sourced by systemd
# --user units (it isn't).

# --- 6. gh CLI (GitHub) -------------------------------------------------------
log "gh CLI"
if ! have gh; then
    sudo install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
        sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
    sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
        sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
    sudo apt-get update -y
    sudo apt-get install -y gh
fi

# --- 7. Tailscale (install only - `tailscale up` is a manual, interactive step)
log "Tailscale"
if ! have tailscale; then
    curl -fsSL https://tailscale.com/install.sh | sh
fi

# --- 8. Data directory, 0700 --------------------------------------------------
log "data directory"
mkdir -p "$KESTREL_DATA"
chmod 0700 "$KESTREL_DATA"

mkdir -p "$CONFIG_DIR"
chmod 0700 "$CONFIG_DIR"

mkdir -p "$HOME/kestrel/releases"

# --- 9. Clone the repo (if not already) --------------------------------------
log "repo checkout"
if [ ! -d "$HOME/kestrel/src/.git" ]; then
    if [ -n "${KESTREL_REPO_URL:-}" ]; then
        git clone "$KESTREL_REPO_URL" "$HOME/kestrel/src"
    else
        echo "KESTREL_REPO_URL not set and $HOME/kestrel/src doesn't exist yet -" >&2
        echo "skipping clone. Set KESTREL_REPO_URL and re-run, or clone it" >&2
        echo "yourself, before running deploy/deploy.sh." >&2
    fi
fi

# --- 10. Install systemd --user units ----------------------------------------
log "systemd user units"
mkdir -p "$SYSTEMD_USER_DIR"
cp "$(dirname "$0")/systemd/kestrel-session-host.service" "$SYSTEMD_USER_DIR/"
cp "$(dirname "$0")/systemd/kestrel-server.service" "$SYSTEMD_USER_DIR/"
cp "$(dirname "$0")/systemd/kestrel-backup.service" "$SYSTEMD_USER_DIR/"
cp "$(dirname "$0")/systemd/kestrel-backup.timer" "$SYSTEMD_USER_DIR/"
systemctl --user daemon-reload

# User units only run in the background without an active login session if
# lingering is enabled - essential on a headless box that boots with nobody
# logged in.
if ! loginctl show-user "$KESTREL_USER" -p Linger 2>/dev/null | grep -q "yes"; then
    sudo loginctl enable-linger "$KESTREL_USER"
fi

echo
echo "###############################################################"
echo "# pi-setup.sh finished the parts it can do unattended.         #"
echo "###############################################################"
echo
echo "Manual steps remaining:"
echo
echo "  1. Tailscale login:"
echo "       sudo tailscale up"
echo "     (follow the URL it prints; approve the device in the admin console)"
echo
echo "  2. Claude Code login (uses Callum's Max account, opens a browser URL"
echo "     to approve from another device since this box is headless):"
echo "       claude"
echo
echo "  3. gh CLI auth (only needed if sessions open/manage PRs via gh under"
echo "     this account rather than a bare git remote):"
echo "       gh auth login"
echo
echo "  4. Secrets file - copy the template, fill it in, lock it down:"
echo "       cp $(dirname "$0")/kestrel.env.example $CONFIG_DIR/kestrel.env"
echo "       chmod 600 $CONFIG_DIR/kestrel.env"
echo "       \$EDITOR $CONFIG_DIR/kestrel.env"
echo
echo "  5. If \$HOME/kestrel/src wasn't cloned automatically above:"
echo "       git clone <repo-url> \$HOME/kestrel/src"
echo
echo "  6. First deploy (checks out a tag, builds its venv, starts the server):"
echo "       \$HOME/kestrel/src/deploy/deploy.sh <tag>"
echo "     Then start the session host once (deploy.sh never touches it):"
echo "       systemctl --user enable --now kestrel-session-host.service"
echo
echo "  7. Enable nightly backups:"
echo "       systemctl --user enable --now kestrel-backup.timer"
echo
echo "  8. Tailscale Serve, so the web app is reachable over tailnet HTTPS"
echo "     (never Funnel - see deploy/README.md):"
echo "       sudo tailscale serve --bg 127.0.0.1:8099"
