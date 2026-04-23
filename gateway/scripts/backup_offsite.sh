#!/usr/bin/env bash
# backup_offsite.sh — weekly encrypted offsite push.
#
# Reads env:
#   BACKUP_GPG_RECIPIENT       — GPG key identity (email or fingerprint)
#                                to encrypt with. REQUIRED.
#   BACKUP_OFFSITE_RSYNC_TARGET — e.g. "user@host:/backups/narve/"
#                                REQUIRED.
#   BACKUP_OFFSITE_RSYNC_OPTS  — extra rsync flags (default: "-e ssh")
#   BACKUP_OFFSITE_RETENTION_WEEKS — default 12 (≈3 months)
#
# GPG key must already be trusted locally (`gpg --edit-key … trust`).
# Fail loudly if any required env var is missing — we don't want a
# silent no-op for backups.

set -euo pipefail

: "${BACKUP_GPG_RECIPIENT:?set BACKUP_GPG_RECIPIENT=<email|fingerprint>}"
: "${BACKUP_OFFSITE_RSYNC_TARGET:?set BACKUP_OFFSITE_RSYNC_TARGET=user@host:/path/}"

DB_PATH="${DB_PATH:-$(dirname "$0")/../auth.db}"
RSYNC_OPTS="${BACKUP_OFFSITE_RSYNC_OPTS:--e ssh}"
WEEKS="${BACKUP_OFFSITE_RETENTION_WEEKS:-12}"

if [[ ! -f "$DB_PATH" ]]; then
    echo "DB not found at $DB_PATH" >&2
    exit 1
fi

stamp=$(date +%Y%m%d)
tmp="$(mktemp "${TMPDIR:-/tmp}/auth.db.$stamp.XXXX")"
enc="$tmp.gpg"

cleanup() { rm -f "$tmp" "$enc" 2>/dev/null || true; }
trap cleanup EXIT

sqlite3 "$DB_PATH" ".backup $tmp"

# GPG encrypts to the recipient's public key. The attacker model here
# is "backup server compromised" — even if the rsync target or the
# transport is hostile, they get ciphertext.
gpg --batch --yes --trust-model always \
    --encrypt --recipient "$BACKUP_GPG_RECIPIENT" \
    --output "$enc" "$tmp"

# Push — we intentionally do NOT --delete on the remote. Retention is
# applied below via a remote find + delete so the transport is purely
# additive and can't wipe the archive if the local script misbehaves.
rsync -az $RSYNC_OPTS "$enc" "$BACKUP_OFFSITE_RSYNC_TARGET"

# Best-effort remote retention. If the target is a bare-bucket
# (S3/B2), this step is a no-op and the offsite provider should
# enforce its own lifecycle rule.
target_host="${BACKUP_OFFSITE_RSYNC_TARGET%%:*}"
target_path="${BACKUP_OFFSITE_RSYNC_TARGET#*:}"
if [[ "$target_host" != "$BACKUP_OFFSITE_RSYNC_TARGET" ]]; then
    # Proper host:path split — we can SSH in and rotate.
    ssh "$target_host" "find '$target_path' -name 'auth.db.*.gpg' -mtime +$((WEEKS * 7)) -delete" || true
fi

printf '%s  offsite snapshot  %s\n' \
    "$(date -Iseconds)" "$BACKUP_OFFSITE_RSYNC_TARGET"
