#!/usr/bin/env bash
# Deploy a tagged release of Kestrel to the Pi: fetch, check out into a
# versioned release dir, build that release's own venv, flip `current` to
# point at it, restart ONLY the server unit, health-check, and roll back
# automatically on failure.
#
# The session host is never touched by a normal deploy - that's the entire
# point of the split (agent/kestrel_agent/host.py). Pass --restart-host to
# also restart it, which WILL kill every running Claude Code session; you
# will be asked to confirm unless --yes is also given.
#
# Usage:
#   deploy/deploy.sh <tag>
#   deploy/deploy.sh <tag> --restart-host [--yes]
#
# Layout on the Pi (created by pi-setup.sh / the first run of this script):
#   ~/kestrel/src/                  full clone, fetch source, worktrees live here
#   ~/kestrel/releases/<tag>/       a `git worktree` checkout of that tag + its own venv
#   ~/kestrel/current -> releases/<tag>   symlink the systemd units execute against
set -euo pipefail

KESTREL_HOME="${KESTREL_HOME:-$HOME/kestrel}"
REPO_DIR="$KESTREL_HOME/src"
RELEASES_DIR="$KESTREL_HOME/releases"
CURRENT_LINK="$KESTREL_HOME/current"
KESTREL_DATA="${KESTREL_DATA:-$HOME/.kestrel}"
KEEP_RELEASES=5
HEALTH_URL="${KESTREL_HEALTH_URL:-http://127.0.0.1:8099/health}"

log() { printf '[%s] %s\n' "$(date -Is)" "$*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

TAG="${1:-}"
[ -n "$TAG" ] || fail "usage: deploy.sh <tag> [--restart-host [--yes]]"
shift || true

RESTART_HOST=false
ASSUME_YES=false
for arg in "$@"; do
    case "$arg" in
        --restart-host) RESTART_HOST=true ;;
        --yes) ASSUME_YES=true ;;
        *) fail "unknown argument: $arg" ;;
    esac
done

[ -d "$REPO_DIR/.git" ] || fail "$REPO_DIR is not a git checkout - run pi-setup.sh first"

log "fetching tags"
git -C "$REPO_DIR" fetch --tags --force origin
git -C "$REPO_DIR" rev-parse -q --verify "refs/tags/${TAG}" >/dev/null \
    || fail "tag '$TAG' not found after fetch"

RELEASE_DIR="$RELEASES_DIR/$TAG"
mkdir -p "$RELEASES_DIR"

if [ -d "$RELEASE_DIR" ]; then
    log "release dir for $TAG already exists - reusing (idempotent re-run)"
else
    log "checking out $TAG into $RELEASE_DIR"
    git -C "$REPO_DIR" worktree add --detach "$RELEASE_DIR" "refs/tags/${TAG}"
fi

log "installing dependencies into ${RELEASE_DIR}/venv"
uv venv --python 3.12 "$RELEASE_DIR/venv" >/dev/null
# --python here is the venv's own interpreter, not a version spec - uv pip
# install needs to be told which venv, since we're not using `uv run`/activate.
uv pip install --python "$RELEASE_DIR/venv/bin/python" \
    -e "$RELEASE_DIR/agent" -e "$RELEASE_DIR/server"

# Built once per release, alongside the venv - server/kestrel/web_static.py
# serves this from the running server itself (KESTREL_WEB_DIST defaults to
# <release>/web/dist, see config.py), which is what makes the tailnet URL
# `tailscale serve` fronts double as the PWA's install source. pi-setup.sh
# installs Node; if it's missing here the release is still usable as an API
# only (the server logs and serves API routes with no shell), so this warns
# rather than failing the whole deploy over a missing frontend toolchain.
if command -v npm >/dev/null 2>&1; then
    log "building web app"
    (cd "$RELEASE_DIR/web" && npm ci && npm run build)
else
    log "WARNING: npm not found - skipping the web build. The server will run" \
        "API-only until Node is installed and this release is redeployed" \
        "(or KESTREL_WEB_DIST is pointed at a build done elsewhere)."
fi

# Capture what "current" points at BEFORE moving it, so a failed health check
# below has something to roll back to. Empty on a first-ever deploy.
PREV_RELEASE=""
if [ -L "$CURRENT_LINK" ]; then
    PREV_RELEASE="$(readlink -f "$CURRENT_LINK")"
fi

log "pointing current -> $RELEASE_DIR"
ln -sfn "$RELEASE_DIR" "${CURRENT_LINK}.tmp"
mv -Tf "${CURRENT_LINK}.tmp" "$CURRENT_LINK"

log "restarting kestrel-server.service"
systemctl --user restart kestrel-server.service

rollback() {
    if [ -n "$PREV_RELEASE" ]; then
        log "ROLLING BACK current -> $PREV_RELEASE"
        ln -sfn "$PREV_RELEASE" "${CURRENT_LINK}.tmp"
        mv -Tf "${CURRENT_LINK}.tmp" "$CURRENT_LINK"
        systemctl --user restart kestrel-server.service
        log "rolled back. $TAG is checked out at $RELEASE_DIR for inspection but not live."
    else
        log "no previous release to roll back to (this was the first deploy) - leaving $TAG in place, but it is unhealthy. Check: systemctl --user status kestrel-server.service"
    fi
}

log "health-checking $HEALTH_URL"
TOKEN=""
[ -f "$KESTREL_DATA/token" ] && TOKEN="$(cat "$KESTREL_DATA/token")"
ok=false
for _ in $(seq 1 15); do
    if curl -fsS -H "Authorization: Bearer ${TOKEN}" "$HEALTH_URL" >/dev/null 2>&1; then
        ok=true
        break
    fi
    sleep 1
done

if ! $ok; then
    log "health check failed after 15s"
    rollback
    exit 1
fi

log "health check passed - $TAG is live"

# Prune old worktrees so releases/ doesn't grow forever. Keeps the currently
# live one plus the $KEEP_RELEASES most recently created before it.
mapfile -t old_releases < <(
    find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | cut -d' ' -f2- | tail -n +$((KEEP_RELEASES + 1))
)
for dir in "${old_releases[@]:-}"; do
    [ -n "$dir" ] || continue
    dir="${dir%/}"
    [ "$dir" = "$RELEASE_DIR" ] && continue
    log "pruning old release $dir"
    git -C "$REPO_DIR" worktree remove --force "$dir" 2>/dev/null || rm -rf "$dir"
done
git -C "$REPO_DIR" worktree prune

if $RESTART_HOST; then
    echo
    echo "!!  --restart-host was passed: this KILLS every running Claude Code"
    echo "!!  session and PTY. There is no undo for sessions in flight."
    if ! $ASSUME_YES; then
        read -r -p "Type 'yes' to restart kestrel-session-host.service: " confirm
        [ "$confirm" = "yes" ] || fail "aborted - session host was not restarted"
    fi
    log "restarting kestrel-session-host.service"
    systemctl --user restart kestrel-session-host.service
fi
