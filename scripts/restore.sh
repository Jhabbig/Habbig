#!/usr/bin/env bash
# restore.sh — operator-driven restore of a single SQLite snapshot.
#
# Symmetric to scripts/backup.sh: takes a gzipped or raw .db/.sqlite file
# produced by either backup.sh or gateway/scripts/backup_*.sh, validates it,
# stages it under gateway/backups/restore_staging/, and waits for explicit
# operator confirmation before overwriting prod.
#
# Usage:
#   scripts/restore.sh <backup-file>
#   scripts/restore.sh gateway/backups/auth_20260514_030000.db.gz
#
# Override defaults via env:
#   HABBIG_ROOT=/some/path scripts/restore.sh <file>
#
# Exit codes:
#   0 — restore completed (or staged + operator aborted at prompt)
#   1 — usage / missing args
#   2 — file not found / not a sqlite file
#   3 — integrity check failed on staged copy
#   4 — operator aborted at prompt

set -euo pipefail

HABBIG_ROOT="${HABBIG_ROOT:-${HOME}/Habbig}"
STAGING_DIR="${HABBIG_ROOT}/gateway/backups/restore_staging"

# ── arg parsing ─────────────────────────────────────────────────────────────
if [[ $# -ne 1 ]]; then
    echo "usage: $0 <backup-file>" >&2
    echo "  e.g. $0 ${HABBIG_ROOT}/gateway/backups/auth_20260514_030000.db.gz" >&2
    exit 1
fi

SRC="$1"
if [[ ! -f "$SRC" ]]; then
    echo "error: backup file not found: $SRC" >&2
    exit 2
fi

# ── stage ───────────────────────────────────────────────────────────────────
# Decompress (if needed) into a staging dir that is OUTSIDE the live DB path.
# We never touch the live DB until the operator confirms.
mkdir -p "$STAGING_DIR"
base="$(basename "$SRC")"
staged="${STAGING_DIR}/${base%.gz}"

if [[ "$SRC" == *.gz ]]; then
    gunzip -c "$SRC" > "$staged"
else
    cp "$SRC" "$staged"
fi

# ── validate ────────────────────────────────────────────────────────────────
# 1. SQLite file format check — the first 16 bytes are "SQLite format 3\0".
header="$(head -c 16 "$staged" 2>/dev/null || true)"
if [[ "$header" != $'SQLite format 3\x00' ]]; then
    echo "error: ${staged} is not a valid SQLite file (bad header)" >&2
    rm -f "$staged"
    exit 2
fi

# 2. PRAGMA integrity_check — must print 'ok'. Anything else (corrupt indexes,
#    bad FKs, partial pages) means the snapshot itself is unrestorable.
integrity="$(sqlite3 "$staged" 'PRAGMA integrity_check;' 2>&1 || true)"
if [[ "$integrity" != "ok" ]]; then
    echo "error: integrity_check failed on ${staged}:" >&2
    echo "$integrity" >&2
    exit 3
fi
echo "staged: $staged"
echo "integrity_check: ok"

# ── determine target ────────────────────────────────────────────────────────
# Filename convention from backup.sh:
#   auth_<TS>.db        → gateway/auth.db
#   voters_<TS>.sqlite  → voters-dashboard/voters.sqlite
#   whale_<TS>.sqlite   → whale-dashboard/whale.sqlite
fname="$(basename "$staged")"
case "$fname" in
    auth_*)
        TARGET="${HABBIG_ROOT}/gateway/auth.db"
        ;;
    voters_*)
        TARGET="${HABBIG_ROOT}/voters-dashboard/voters.sqlite"
        ;;
    whale_*)
        TARGET="${HABBIG_ROOT}/whale-dashboard/whale.sqlite"
        ;;
    *)
        echo "error: cannot infer restore target from filename '${fname}'" >&2
        echo "expected prefix one of: auth_, voters_, whale_" >&2
        exit 2
        ;;
esac

# ── confirm ─────────────────────────────────────────────────────────────────
echo ""
echo "================================================================"
echo "  About to overwrite live database:"
echo "    target:  $TARGET"
echo "    source:  $staged"
echo "    size:    $(stat -f%z "$staged" 2>/dev/null || stat -c%s "$staged") bytes"
echo ""
echo "  This will:"
echo "    1. Move the current live DB aside as ${TARGET}.preremove-\$(date +%s)"
echo "    2. Copy the staged file into ${TARGET}"
echo "    3. NOT restart any services — do that yourself afterwards."
echo "================================================================"
echo ""
read -r -p "Type RESTORE to confirm: " confirm
if [[ "$confirm" != "RESTORE" ]]; then
    echo "aborted by operator; staged file left at $staged for inspection."
    exit 4
fi

# ── execute ─────────────────────────────────────────────────────────────────
if [[ -f "$TARGET" ]]; then
    preremove="${TARGET}.preremove-$(date +%s)"
    mv "$TARGET" "$preremove"
    echo "moved live DB aside: $preremove"
fi
cp "$staged" "$TARGET"
echo "restore complete: $TARGET"
echo ""
echo "next steps:"
echo "  1. systemctl restart narve-gateway        # or relevant subproduct"
echo "  2. curl -s http://127.0.0.1:7000/health"
echo "  3. sqlite3 ${TARGET} 'PRAGMA integrity_check'"
echo "  4. once verified, delete preremove file:  rm ${preremove:-<n/a>}"
