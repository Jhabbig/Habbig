#!/usr/bin/env bash
# backup_daily.sh — once-a-day compressed snapshot, 30-day retention.
#
# Runs on top of the hourly cadence so the oldest data any snapshot
# can surface is 1 day old (hourly handles fresher recovery). Daily
# snapshots are gzip -9 to keep them cheap on disk.
#
# Install via `scripts/install_backup_cron.sh`, or run manually:
#   scripts/backup_daily.sh

set -euo pipefail

DB_PATH="${DB_PATH:-$(dirname "$0")/../auth.db}"
BACKUP_DIR="${BACKUP_DAILY_DIR:-/var/backups/narve/daily}"
RETENTION_DAYS="${RETENTION_DAILY_DAYS:-30}"

if [[ ! -f "$DB_PATH" ]]; then
    echo "DB not found at $DB_PATH" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp=$(date +%Y%m%d)
tmp="$(mktemp "${TMPDIR:-/tmp}/auth.db.$stamp.XXXX")"

cleanup() { rm -f "$tmp" "$tmp.gz" 2>/dev/null || true; }
trap cleanup EXIT

if ! sqlite3 "$DB_PATH" ".backup $tmp"; then
    echo "backup failed writing to $tmp" >&2
    exit 2
fi

gzip -9 "$tmp"
mv "$tmp.gz" "$BACKUP_DIR/auth.db.$stamp.gz"
trap - EXIT  # successful move — nothing left to clean.

# Delete files older than N days. -mtime +30 matches files modified
# 31+ days ago, so an exact 30-day retention window.
find "$BACKUP_DIR" -name 'auth.db.*.gz' -mtime "+$RETENTION_DAYS" -delete

printf '%s  daily snapshot  %s (%s bytes)\n' \
    "$(date -Iseconds)" \
    "$BACKUP_DIR/auth.db.$stamp.gz" \
    "$(stat -f%z "$BACKUP_DIR/auth.db.$stamp.gz" 2>/dev/null \
       || stat -c%s "$BACKUP_DIR/auth.db.$stamp.gz")"
