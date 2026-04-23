# SEV-1 — Database corruption

Symptoms: `sqlite3` errors in the log (`database disk image is
malformed`, `file is encrypted or is not a database`), `/health`
database check failing, or a queries-are-returning-garbage report
from a user.

## Detect

```bash
sqlite3 ~/Habbig/gateway/auth.db "PRAGMA integrity_check"
```

Clean DB → `ok`. Any other output is a hit. Capture the exact lines
— they name the affected page / row and inform the recovery path.

## Mitigate — freeze, copy, recover, swap

**1. Stop writers.**

```bash
fuser -k 7000/tcp      # kill uvicorn
fuser -k 7001/tcp      # kill staging uvicorn if running
```

Verify nothing is holding the DB:

```bash
lsof ~/Habbig/gateway/auth.db
```

**2. Copy aside.** Keep the corrupt file for forensics.

```bash
cp ~/Habbig/gateway/auth.db ~/Habbig/gateway/auth.db.corrupted.$(date +%s)
```

**3. Recover with SQLite's built-in tool.**

```bash
sqlite3 ~/Habbig/gateway/auth.db.corrupted.<ts> ".recover" > /tmp/recovered.sql
sqlite3 /tmp/recovered.db < /tmp/recovered.sql
```

`.recover` walks the on-disk B-trees and dumps everything it can
read as SQL, skipping unreadable pages. Expect some row loss on
heavily corrupted pages; a clean B-tree loses nothing.

**4. Verify the recovered DB.**

```bash
sqlite3 /tmp/recovered.db "PRAGMA integrity_check"
# must read: ok
```

Spot-check row counts against the last known-good numbers (read
them from the admin panel the day before, or from the most recent
backup). Critical tables to verify:

```bash
for t in users sessions subscriptions predictions source_credibility \
         saved_predictions followed_sources takes takes_votes \
         processed_stripe_events; do
  echo -n "$t: "
  sqlite3 /tmp/recovered.db "SELECT COUNT(*) FROM $t;"
done
```

Unusual drops (more than a few %) against yesterday's numbers are a
signal to escalate to the backup-restore path below rather than
trust this recovery.

**5. Swap in.**

```bash
mv ~/Habbig/gateway/auth.db ~/Habbig/gateway/auth.db.corrupted.pre-swap.$(date +%s)
cp /tmp/recovered.db ~/Habbig/gateway/auth.db
```

**6. Restart.**

```bash
cd ~/Habbig/gateway
set -a; source ~/.gateway_env; set +a
export PRODUCTION=1
nohup python3 -m uvicorn server:app --host 127.0.0.1 --port 7000 \
  > /tmp/gateway.log 2>&1 &
disown
sleep 5
curl -s -H "CF-Connecting-IP: 127.0.0.1" http://127.0.0.1:7000/health
```

## If recovery fails

Restore from the most recent backup. See the backup-restore section
in [`../RUNBOOK.md`](../RUNBOOK.md). Users lose data between the
last backup and the corruption timestamp — tell them in a /status
post and consider bucketed emails to heavy users.

## Preserve evidence

**Do not delete** any `auth.db.corrupted.*` files until the
postmortem is complete. Keep them in `~/Habbig/gateway/` with their
epoch-suffix names — the postmortem may need to reference
`PRAGMA integrity_check` output from the original on-disk state.

## Common root causes to check in the postmortem

* **Disk full.** `df -h ~/Habbig`. WAL flushes fail silently on
  ENOSPC and can leave inconsistent pages.
* **Power loss / kernel OOM.** `journalctl -k --since "1 hour ago" |
  grep -i "oom\|kill"`.
* **Concurrent writers from misconfigured scaling.** One of the
  dashboard backends accidentally opening `auth.db` with write mode.
  Grep for `sqlite3.connect.*auth.db` across the repo if you suspect.
* **Filesystem corruption on the host.** `dmesg | grep -i "ext4\|
  btrfs"`.

## Prevention

* Daily backup cron (already running; see [`../RUNBOOK.md`](../RUNBOOK.md)
  backup section).
* `PRAGMA journal_mode=WAL` already set — never revert.
* Nightly `VACUUM` / `PRAGMA wal_checkpoint(TRUNCATE)` via
  `jobs/db_maintenance.py`.

## Postmortem

Required within 48 hours for any SEV-1. Template:
[`postmortem_template.md`](postmortem_template.md).
