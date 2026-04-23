#!/usr/bin/env bash
# backup_verify.sh — pick the newest daily archive, restore it to a
# temp path, run integrity_check + foreign_key_check. If either trips,
# write an alert line to stdout (cron captures, mail plumbs to ops).
#
# Exit 0 when all good, 1 when integrity fails, 2 when no backup found.
#
# Runs weekly from cron alongside the offsite push, or on demand:
#   scripts/backup_verify.sh

set -euo pipefail

BACKUP_DIR="${BACKUP_DAILY_DIR:-/var/backups/narve/daily}"

if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "BACKUP VERIFY: no backup dir at $BACKUP_DIR"
    exit 2
fi

latest="$(ls -t "$BACKUP_DIR"/auth.db.*.gz 2>/dev/null | head -1 || true)"
if [[ -z "$latest" ]]; then
    echo "BACKUP VERIFY: no daily archives in $BACKUP_DIR"
    exit 2
fi

restore="$(mktemp "${TMPDIR:-/tmp}/restore_test.XXXX.db")"
trap 'rm -f "$restore"' EXIT

gunzip -c "$latest" > "$restore"

integrity="$(sqlite3 "$restore" 'PRAGMA integrity_check' 2>&1 | head -3 | tr '\n' ';')"
fk_viol="$(sqlite3 "$restore" 'PRAGMA foreign_key_check' 2>&1 | wc -l | tr -d ' ')"

if [[ "$integrity" != "ok;" ]] || [[ "$fk_viol" -gt 0 ]]; then
    echo "BACKUP VERIFY FAIL: $latest"
    echo "  integrity=$integrity"
    echo "  foreign_key_check_rows=$fk_viol"
    exit 1
fi

printf 'BACKUP VERIFY OK: %s (size=%s bytes)\n' \
    "$latest" \
    "$(stat -f%z "$latest" 2>/dev/null || stat -c%s "$latest")"
