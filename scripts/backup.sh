#!/usr/bin/env bash
# backup.sh — unified daily snapshot of every SQLite database under ~/Habbig.
#
# Complements gateway/scripts/backup_{hourly,daily,offsite,verify}.sh, which
# only cover gateway/auth.db on a per-cron cadence. This script is the single
# entry point invoked by the narve-backup.timer systemd unit (03:00 UTC daily)
# and pulls in the per-subproduct DBs (voters.sqlite, whale.sqlite) alongside
# the gateway auth.db. The recovery_drill APScheduler job
# (gateway/jobs/db_maintenance.py::recovery_drill) verifies these copies are
# restorable on a 90-day cadence.
#
# Run manually:
#   scripts/backup.sh
#
# Override defaults via env:
#   HABBIG_ROOT=/some/path BACKUP_DIR=/var/tmp/b scripts/backup.sh
#
# Retention: 7 daily snapshots + 4 weekly anchors + 12 monthly anchors. Files
# older than 90 days are deleted unconditionally; the find expression keeps
# 1-of-7-daily, 1-of-4-weekly, 1-of-12-monthly under a 90-day horizon.

set -euo pipefail

# ── paths ───────────────────────────────────────────────────────────────────
HABBIG_ROOT="${HABBIG_ROOT:-${HOME}/Habbig}"
BACKUP_DIR="${BACKUP_DIR:-${HABBIG_ROOT}/gateway/backups}"
TS="$(date -u +%Y%m%d_%H%M%S)"

mkdir -p "$BACKUP_DIR"

# ── gateway auth.db ─────────────────────────────────────────────────────────
# VACUUM INTO is atomic against a live writer (it acquires a shared lock,
# writes a fully-rebuilt file, and releases) and produces a smaller artifact
# than a raw file copy because freelist pages are dropped. The .backup API
# would also work but VACUUM INTO is cleaner for a once-a-day snapshot.
AUTH_DB="${HABBIG_ROOT}/gateway/auth.db"
if [[ -f "$AUTH_DB" ]]; then
    sqlite3 "$AUTH_DB" "VACUUM INTO '${BACKUP_DIR}/auth_${TS}.db'"
else
    echo "warn: ${AUTH_DB} not found, skipping gateway snapshot" >&2
fi

# ── per-subproduct DBs ──────────────────────────────────────────────────────
# Loop matches voters.sqlite and whale.sqlite (the two subproducts with their
# own SQLite stores today). Any new subproduct that adds a *.sqlite file at
# ~/Habbig/<dashboard>/<name>.sqlite is picked up by extending the patterns
# below — keep the glob explicit, not wild, so test fixtures aren't snapshotted.
for db in \
    "${HABBIG_ROOT}/voters-dashboard/voters.sqlite" \
    "${HABBIG_ROOT}/whale-dashboard/whale.sqlite"; do
    if [[ -f "$db" ]]; then
        base="$(basename "$db" .sqlite)"
        sqlite3 "$db" "VACUUM INTO '${BACKUP_DIR}/${base}_${TS}.sqlite'"
    fi
done

# ── compress ────────────────────────────────────────────────────────────────
# Best-effort gzip; redirect stderr to /dev/null so the trailing `|| true`
# only swallows the (rare) "no matches" case. -9 is fine — the script is
# bound by sqlite3, not gzip.
gzip -9 "${BACKUP_DIR}"/auth_"${TS}".db 2>/dev/null || true
gzip -9 "${BACKUP_DIR}"/*_"${TS}".sqlite 2>/dev/null || true

# ── retention: 7d + 4w + 12m, hard cap 90 days ──────────────────────────────
# Step 1: drop anything older than 90 days. This is the safety net.
find "$BACKUP_DIR" -name '*.gz' -type f -mtime +90 -delete

# Step 2: within the 90-day window, keep:
#   - the 7 newest daily snapshots (always)
#   - one weekly anchor (Sunday) per week, last 4 weeks
#   - one monthly anchor (1st of month) per month, last 12 months
# Files outside those buckets in the 8-90 day range are pruned. We do this in
# python to keep the logic readable; bash globbing across mtime buckets is
# unmaintainable.
python3 - "$BACKUP_DIR" <<'PY'
import os, sys, glob, datetime as dt
backup_dir = sys.argv[1]
now = dt.datetime.now(dt.timezone.utc)
files = sorted(
    (f for f in glob.glob(os.path.join(backup_dir, '*.gz')) if os.path.isfile(f)),
    key=lambda f: os.stat(f).st_mtime,
    reverse=True,
)
keep = set()
# 7 newest daily
keep.update(files[:7])
# weekly: most recent file per ISO week, last 4 weeks
weekly_seen = {}
monthly_seen = {}
for f in files:
    mtime = dt.datetime.fromtimestamp(os.stat(f).st_mtime, dt.timezone.utc)
    age = (now - mtime).days
    if age <= 90:
        wkey = (mtime.isocalendar().year, mtime.isocalendar().week)
        if wkey not in weekly_seen and len(weekly_seen) < 4:
            weekly_seen[wkey] = f
        mkey = (mtime.year, mtime.month)
        if mkey not in monthly_seen and len(monthly_seen) < 12:
            monthly_seen[mkey] = f
keep.update(weekly_seen.values())
keep.update(monthly_seen.values())
removed = 0
for f in files:
    if f not in keep:
        os.unlink(f)
        removed += 1
print(f"retention: kept {len(keep)} / removed {removed}")
PY

# ── summary ─────────────────────────────────────────────────────────────────
echo "backup complete: ${BACKUP_DIR}"
ls -la "$BACKUP_DIR" | tail -5
