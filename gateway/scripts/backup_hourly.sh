#!/usr/bin/env bash
# backup_hourly.sh — take a point-in-time snapshot of auth.db every hour.
#
# Uses SQLite's online .backup command (wraps sqlite3_backup API), which
# is safe to run against a live WAL-mode database. Retains the last 24
# snapshots — older ones get swept.
#
# Install via `scripts/install_backup_cron.sh`, or run manually:
#   scripts/backup_hourly.sh           # uses defaults
#   BACKUP_DIR=/tmp/b scripts/backup_hourly.sh

set -euo pipefail

DB_PATH="${DB_PATH:-$(dirname "$0")/../auth.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/narve}"
RETENTION="${RETENTION_HOURLY:-24}"

if [[ ! -f "$DB_PATH" ]]; then
    echo "DB not found at $DB_PATH" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp=$(date +%Y%m%d_%H%M)
out="$BACKUP_DIR/auth.db.$stamp"

# .backup runs in a serialised writer lock, so two concurrent hourly
# crons would just queue — no corruption possible. Bail loudly if the
# command exits non-zero so monitoring catches silent failures.
if ! sqlite3 "$DB_PATH" ".backup $out"; then
    echo "backup failed: $out" >&2
    exit 2
fi

# Retention — keep only the N most recent hourly files, drop the rest.
# `ls -t` is MTIME-ordered so rotation handles clock skew gracefully.
ls -t "$BACKUP_DIR"/auth.db.* 2>/dev/null | tail -n "+$((RETENTION + 1))" | xargs -r rm --

printf '%s  hourly snapshot  %s (%s bytes)\n' \
    "$(date -Iseconds)" "$out" "$(stat -f%z "$out" 2>/dev/null || stat -c%s "$out")"
