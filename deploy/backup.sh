#!/usr/bin/env bash
# Kestrel nightly backup: SQLite (online backup, WAL-safe) + the memory repo,
# both pushed to a private git remote. Run by kestrel-backup.timer, or
# by hand for an ad-hoc backup before something risky.
#
# NEVER touches $KESTREL_DATA/token or the kestrel.env secrets file - only
# kestrel.db and the memory/ repo are ever read.
#
# Safe to re-run: each run overwrites the same working files and makes at
# most one commit (skipped if nothing changed), so re-running after a
# failure doesn't pile up junk commits or duplicate data.
set -euo pipefail

KESTREL_DATA="${KESTREL_DATA:-$HOME/.kestrel}"
DB_PATH="${KESTREL_DATA}/kestrel.db"
MEMORY_REPO="${KESTREL_DATA}/memory"
BACKUP_DIR="${KESTREL_DATA}/backups"
MEMORY_BACKUP_REMOTE="${MEMORY_BACKUP_REMOTE:-${BACKUP_REMOTE:-}}"

log() { printf '[%s] %s\n' "$(date -Is)" "$*" >&2; }
fail() { log "ERROR: $*"; exit 1; }

[ -n "${BACKUP_REMOTE:-}" ] || fail "BACKUP_REMOTE is not set (see deploy/kestrel.env.example)"
command -v sqlite3 >/dev/null || fail "sqlite3 not installed"
command -v git >/dev/null || fail "git not installed"

# --- 1. SQLite: online backup, not a file copy ------------------------------
#
# kestrel.db runs in WAL mode (server/kestrel/db.py) with the server live and
# writing to it. `cp`/`rsync` on a WAL database can copy the main file
# mid-checkpoint and miss data sitting in -wal, producing a backup that opens
# fine but is silently missing recent writes. `sqlite3 .backup` uses SQLite's
# own online backup API, which takes a consistent snapshot regardless of
# concurrent writers - this is the one correct way to do this without
# stopping the server.
if [ -f "$DB_PATH" ]; then
    mkdir -p "$BACKUP_DIR"
    log "backing up $DB_PATH"
    sqlite3 "$DB_PATH" ".backup '${BACKUP_DIR}/kestrel.db.tmp'"
    mv "${BACKUP_DIR}/kestrel.db.tmp" "${BACKUP_DIR}/kestrel.db"
else
    log "no database at $DB_PATH yet - skipping SQLite backup"
fi

# --- 2. Push the backup dir (SQLite snapshot) to its own git history --------
#
# One file, overwritten every night, committed into git: git's own history
# is the timestamped archive (every prior night's snapshot is a commit you
# can check out), so this doesn't also need to manage a rotation of dated
# copies on disk.
if [ -d "$BACKUP_DIR" ]; then
    if [ ! -d "${BACKUP_DIR}/.git" ]; then
        git -C "$BACKUP_DIR" init -q -b main
        git -C "$BACKUP_DIR" remote add origin "$BACKUP_REMOTE"
    fi
    git -C "$BACKUP_DIR" add kestrel.db
    if ! git -C "$BACKUP_DIR" diff --cached --quiet; then
        git -C "$BACKUP_DIR" -c user.email=kestrel@localhost -c user.name=kestrel-backup \
            commit -q -m "backup: $(date -Is)"
        log "pushing SQLite backup to $BACKUP_REMOTE"
        git -C "$BACKUP_DIR" push -q origin main
    else
        log "SQLite backup unchanged since last run - not committing"
    fi
fi

# --- 3. Push the memory repo -------------------------------------------------
#
# The memory repo is committed locally on every write already (Kestrel's own
# job, not this script's) - this only adds/refreshes the backup remote and
# pushes whatever is already committed there.
if [ -d "${MEMORY_REPO}/.git" ]; then
    if [ -n "$MEMORY_BACKUP_REMOTE" ]; then
        if git -C "$MEMORY_REPO" remote get-url backup >/dev/null 2>&1; then
            git -C "$MEMORY_REPO" remote set-url backup "$MEMORY_BACKUP_REMOTE"
        else
            git -C "$MEMORY_REPO" remote add backup "$MEMORY_BACKUP_REMOTE"
        fi
        branch="$(git -C "$MEMORY_REPO" branch --show-current)"
        log "pushing memory repo to $MEMORY_BACKUP_REMOTE"
        git -C "$MEMORY_REPO" push -q backup "${branch:-HEAD}:refs/heads/${branch:-main}"
    else
        log "no MEMORY_BACKUP_REMOTE/BACKUP_REMOTE configured for the memory repo - skipping"
    fi
else
    log "no memory repo at $MEMORY_REPO yet - skipping"
fi

log "backup complete"
