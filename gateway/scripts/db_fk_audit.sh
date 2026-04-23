#!/usr/bin/env bash
# db_fk_audit.sh — dump every foreign key across every non-FTS table.
#
# Used to validate ON DELETE policy (CASCADE for per-user, SET NULL for
# audit, RESTRICT for reference). See DB_HEALTH.md for the matrix.

set -euo pipefail

DB_PATH="${1:-$(dirname "$0")/../auth.db}"
if [[ ! -f "$DB_PATH" ]]; then
    echo "DB not found at $DB_PATH" >&2
    exit 1
fi

for tbl in $(sqlite3 "$DB_PATH" "
    SELECT name FROM sqlite_master
     WHERE type='table'
       AND name NOT LIKE '%\\_fts%' ESCAPE '\\'
       AND name NOT LIKE '%\\_fts\\_%' ESCAPE '\\'
     ORDER BY name
"); do
    fks=$(sqlite3 "$DB_PATH" "PRAGMA foreign_key_list($tbl)")
    if [[ -n "$fks" ]]; then
        echo "=== $tbl ==="
        echo "$fks"
    fi
done
