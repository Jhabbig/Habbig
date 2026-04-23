# narve.ai Operations Runbook

Last updated: 2026-04-08

> **Note on stack reality.** narve.ai is a FastAPI + SQLite monolith running
> behind a Cloudflare Tunnel on a single Ubuntu VM, NOT a Docker compose
> stack. This runbook reflects that. If you came here looking for
> `docker-compose up`, you're in the wrong place.

## Quick reference

| Thing | Value |
|---|---|
| Production URL | https://narve.ai |
| Staging URL | https://staging.narve.ai |
| Production `/health` | https://narve.ai/health |
| Staging `/health` | https://staging.narve.ai/health |
| Admin panel | https://narve.ai/admin |
| Server (Tailscale) | `100.69.44.108` |
| SSH user | `julianhabbig` |
| Project path | `~/Habbig/gateway` |
| Production port | `7000` |
| Staging port | `7001` |
| Production DB | `~/Habbig/gateway/auth.db` (SQLite, WAL) |
| Staging DB | `~/Habbig/gateway/auth-staging.db` |
| Production env | `~/.gateway_env` |
| Staging env | `~/.gateway_env_staging` |
| Cloudflare Tunnel config | `/etc/cloudflared/config.yml` |
| Tunnel service | `systemctl status cloudflared` |
| Prod log | `/tmp/gateway.log` |
| Staging log | `/tmp/gateway_staging.log` |
| GitHub repo | https://github.com/Jhabbig/Habbig |

## Deployments

### Deploy to staging (automatic on push to main)

GitHub Actions runs `.github/workflows/deploy-staging.yml` on every push to
`main`, but only after `test.yml` passes. Manual path:

```bash
bash scripts/deploy-staging.sh
```

The script runs tests locally, `scp`s files, kills port 7001, restarts
uvicorn with `~/.gateway_env_staging`, and verifies `/health` returns 200.

### Deploy to production (manual only)

1. Verify staging is working:
   ```bash
   curl -s https://staging.narve.ai/health | python3 -m json.tool
   ```
2. From your laptop:
   ```bash
   bash scripts/deploy-production.sh
   ```
   Or from GitHub Actions → **Deploy to Production** → Run workflow → type
   `deploy` in the confirm input.
3. The script refuses to deploy if staging is unhealthy. Override with
   `SKIP_STAGING=1` only if you absolutely know what you're doing.
4. Verify:
   ```bash
   curl -s https://narve.ai/health | python3 -m json.tool
   ```

### Rollback production

```bash
bash scripts/rollback.sh                    # interactive — picks from git log
bash scripts/rollback.sh <commit_hash>       # direct rollback
```

Rollback works by `git checkout`ing the target commit ON THE SERVER and
restarting uvicorn. It does NOT touch your local files.

### Why there's no docker-compose

The prompt template that spawned this runbook assumed Docker + PostgreSQL +
ARQ + Alembic. The real stack is nohup uvicorn + SQLite. Deploy scripts
work accordingly.

## Services

### Start the gateway (production)

```bash
ssh julianhabbig@100.69.44.108
cd ~/Habbig/gateway
set -a; . ~/.gateway_env; set +a
nohup env PRODUCTION=1 python3 -m uvicorn server:app \
    --host 127.0.0.1 --port 7000 > /tmp/gateway.log 2>&1 &
```

### Start the gateway (staging)

```bash
ssh julianhabbig@100.69.44.108
cd ~/Habbig/gateway
set -a; . ~/.gateway_env_staging; set +a
nohup python3 -m uvicorn server:app \
    --host 127.0.0.1 --port 7001 > /tmp/gateway_staging.log 2>&1 &
```

### Stop

```bash
# Production
ssh julianhabbig@100.69.44.108 "fuser -k 7000/tcp"

# Staging
ssh julianhabbig@100.69.44.108 "fuser -k 7001/tcp"
```

### Check status

```bash
# Is prod running?
ssh julianhabbig@100.69.44.108 "pgrep -af 'uvicorn.*7000'"

# Is staging running?
ssh julianhabbig@100.69.44.108 "pgrep -af 'uvicorn.*7001'"

# Is the Cloudflare Tunnel running?
ssh julianhabbig@100.69.44.108 "systemctl status cloudflared --no-pager"
```

### Tail logs

```bash
# Production
ssh julianhabbig@100.69.44.108 "tail -f /tmp/gateway.log"

# Staging
ssh julianhabbig@100.69.44.108 "tail -f /tmp/gateway_staging.log"

# Cloudflare Tunnel
ssh julianhabbig@100.69.44.108 "sudo journalctl -u cloudflared -f"
```

## Scheduled jobs

Every recurring job (health checks, weekly reports, portfolio syncs,
Claude cost rollups, etc.) is driven by APScheduler from inside the
gateway process. The configuration lives in `gateway/scheduler/`:

* `scheduler/scheduler.py` — `Scheduler` wrapper + the singleton.
* `scheduler/registry.py`  — wires every job at startup. Bridges the
  legacy `jobs/*.py` `@register_cron` calls + adds the spec-named
  jobs.
* Migration `105_scheduler_job_runs` owns the `job_runs` audit table.

**⚠️ Never run uvicorn with `--workers > 1` on this host until the
scheduler is moved off-process.** Every worker would instantiate its
own `AsyncIOScheduler` and fire every job N times. The singleton
supports soft leader election via `NARVE_SCHEDULER_LEADER`:

* `NARVE_SCHEDULER_LEADER=1` — this process runs the scheduler (the
  default when the env var is unset).
* `NARVE_SCHEDULER_LEADER=0` — this process skips scheduling. Set on
  every non-leader worker if you ever do run multi-worker uvicorn.
* `NARVE_SKIP_SCHEDULER=1` — unconditionally skip. Used by the test
  harness.

### Inspect + control jobs

Admins can see every registered job at `/admin/jobs`:

* Last run, next run, last duration, avg duration, 24h failure count.
* Pause / resume / trigger-now buttons per job.
* Per-job history view (click a row) of the last 50 runs.

Rollback to the legacy in-process cron loop (pre-APScheduler) by
setting `NARVE_LEGACY_CRON_LOOP=1` on the gateway process. Only use
this if APScheduler is misbehaving and you need breathing room to
diagnose — the two loops will double-fire every job, so don't leave
it on.

### Checking job history from SQL

```sql
-- Last 10 runs across every job
SELECT job_name, started_at, duration_ms, ok, error
FROM job_runs
ORDER BY started_at DESC
LIMIT 10;

-- Failing jobs in the last 24h
SELECT job_name, COUNT(*) AS fails
FROM job_runs
WHERE ok = 0 AND started_at >= strftime('%s', 'now') - 86400
GROUP BY job_name
ORDER BY fails DESC;
```

## Database

The production DB is a single SQLite file (`auth.db`) in WAL mode. There
are no migrations — schema lives in `db.py` under `SCHEMA = """..."""` and
is applied by `db.init_db()` at startup. Lightweight migrations (adding new
columns to existing tables) are done with `ALTER TABLE` probes also in
`init_db`.

### Inspect the database

```bash
ssh julianhabbig@100.69.44.108
cd ~/Habbig/gateway
python3 -c "
import sqlite3
c = sqlite3.connect('auth.db')
c.row_factory = sqlite3.Row
for t in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\"):
    print(t['name'])
"
```

### Count users

```bash
python3 -c "
import sqlite3
c = sqlite3.connect('auth.db')
print('users:', c.execute('SELECT COUNT(*) FROM users').fetchone()[0])
print('sessions:', c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0])
print('invite tokens (unclaimed):', c.execute(\"SELECT COUNT(*) FROM invite_tokens WHERE status='unclaimed'\").fetchone()[0])
"
```

### Backup

```bash
# Hot backup — SQLite's .backup is safe against a running writer
ssh julianhabbig@100.69.44.108 "
    cd ~/Habbig/gateway
    python3 -c \"
import sqlite3, datetime
src = sqlite3.connect('auth.db')
ts = datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')
dst = sqlite3.connect(f'auth.db.backup_{ts}')
src.backup(dst)
dst.close()
src.close()
print(f'wrote auth.db.backup_{ts}')
\"
"
```

### Automated 3-2-1 backup (installed via cron)

Production host runs four crons (see `scripts/install_backup_cron.sh`):

| Cadence | Script | Path | Retention |
|---|---|---|---|
| Hourly at `:07` | `backup_hourly.sh` | `/var/backups/narve/auth.db.YYYYMMDD_HHMM` | 24 snapshots |
| Daily at `03:14` | `backup_daily.sh` | `/var/backups/narve/daily/auth.db.YYYYMMDD.gz` | 30 days |
| Sun `03:42` | `backup_offsite.sh` | encrypted GPG + rsync to offsite target | 12 weeks |
| Mon `04:07` | `backup_verify.sh` | gunzip latest daily, `PRAGMA integrity_check` | log-only |

Offsite requires two env vars in `/etc/default/narve-backup`:

```
BACKUP_GPG_RECIPIENT=backup@narve.ai
BACKUP_OFFSITE_RSYNC_TARGET=user@offsite-host:/backups/narve/
```

See `/admin/backups` for live freshness, last verify result, and
recovery-drill history.

### Restore from backup

**Data-loss tolerance:**

* Hourly snapshot → up to 60 min lost
* Daily snapshot  → up to 24 h lost
* Offsite weekly  → up to 7 days lost

**From latest hourly (< 1h data loss):**

```bash
ssh julianhabbig@100.69.44.108 "
    # 1. Stop the app so nothing writes mid-restore
    sudo fuser -k 7000/tcp || true
    sleep 2
    cd ~/Habbig/gateway

    # 2. Move aside the corrupted/stale files
    mv auth.db         auth.db.corrupted.\$(date +%s)         2>/dev/null || true
    mv auth.db-wal     auth.db-wal.corrupted.\$(date +%s)     2>/dev/null || true
    mv auth.db-shm     auth.db-shm.corrupted.\$(date +%s)     2>/dev/null || true

    # 3. Copy in the latest hourly backup
    LATEST=\$(ls -t /var/backups/narve/auth.db.* | head -1)
    cp \"\$LATEST\" auth.db

    # 4. Verify integrity BEFORE restarting
    sqlite3 auth.db 'PRAGMA integrity_check' | head -3

    # 5. Restart uvicorn (the usual path — see server-commit section)
"
```

Verify `/health` returns 200 and `/admin/backups` reflects the new
freshest hourly once restart completes.

**From a daily archive (< 24h data loss):**

```bash
ssh julianhabbig@100.69.44.108 "
    cd ~/Habbig/gateway
    # Stop + move aside as above, then:
    LATEST=\$(ls -t /var/backups/narve/daily/*.gz | head -1)
    gunzip -c \"\$LATEST\" > auth.db
    sqlite3 auth.db 'PRAGMA integrity_check'
    # Restart uvicorn
"
```

**From the offsite weekly (≤ 7d data loss):**

Pull the `.gpg` file from the offsite provider to a local workstation,
then:

```bash
gpg --decrypt auth.db.YYYYMMDD.gpg > auth.db
sqlite3 auth.db "PRAGMA integrity_check"     # must print 'ok'
scp auth.db julianhabbig@100.69.44.108:~/Habbig/gateway/auth.db.restored
# On the server: stop service, move aside live DB, rename .restored → auth.db, restart.
```

**After ANY restore:**

1. `sqlite3 auth.db "PRAGMA integrity_check"` → must be `ok`.
2. Restart app; check `/health` returns 200.
3. Check `/admin/backups` freshness cards go green.
4. Commit the restored DB binary to the local server branch if that's
   part of your workflow (most hosts keep the DB outside git).

### Recovery drill

Automated quarterly via `jobs/db_maintenance.py::recovery_drill`
(first of Jan/Apr/Jul/Oct, 05:20 UTC). Writes a row to `drill_runs`:

* takes a live snapshot via SQLite's `.backup` API,
* runs `PRAGMA integrity_check` + `PRAGMA foreign_key_check` on the copy,
* compares `COUNT(*)` on `users` + `predictions` — divergence > 1% = FAIL,
* deletes the tmp copy.

History is visible at `/admin/backups § Recovery drills`.

## Scraper

Not yet deployed. The scraper lives at `~/Habbig/scraper/` locally but is
not currently running on the production server. When it is:

```bash
# Check scraper health (when deployed, internal port 8001)
ssh julianhabbig@100.69.44.108 "curl -s http://127.0.0.1:8001/health"

# Restart
ssh julianhabbig@100.69.44.108 "cd ~/Habbig/scraper && bash start.sh"
```

## Markets feature

### Check market connection count
```bash
ssh julianhabbig@100.69.44.108 "cd ~/Habbig/gateway && python3 -c \"
import sqlite3
c = sqlite3.connect('auth.db')
for source, cnt in c.execute('SELECT source, COUNT(*) FROM user_market_credentials GROUP BY source'):
    print(source, cnt)
\""
```

### Set or rotate Kalshi service account
The Kalshi public market listing requires auth. If we want the market list
to show on logged-out users, set `KALSHI_SERVICE_EMAIL` and
`KALSHI_SERVICE_PASSWORD` in `~/.gateway_env` and restart.

### Rotate the Fernet encryption key
⚠️ This invalidates all stored Kalshi tokens — users must reconnect.

```bash
NEW_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
# Put NEW_KEY into ~/.gateway_env, delete the old credentials, restart:
ssh julianhabbig@100.69.44.108 "
    cd ~/Habbig/gateway
    python3 -c 'import sqlite3; sqlite3.connect(\"auth.db\").execute(\"DELETE FROM user_market_credentials WHERE source=\\\"kalshi\\\"\").connection.commit()'
    fuser -k 7000/tcp; sleep 2
    set -a; . ~/.gateway_env; set +a
    nohup env PRODUCTION=1 python3 -m uvicorn server:app --host 127.0.0.1 --port 7000 > /tmp/gateway.log 2>&1 &
"
```

## Email

Email is **synchronous** via `smtplib`. There is no ARQ queue, no worker.
The SMTP call happens on the request thread inside `/forgot-password/email`
and `/api/enquire` handlers.

```bash
# Is SMTP configured?
ssh julianhabbig@100.69.44.108 "grep -c '^SMTP' ~/.gateway_env"

# Send a test from the server
ssh julianhabbig@100.69.44.108 "cd ~/Habbig/gateway && set -a; . ~/.gateway_env; set +a; python3 -c \"
import os, smtplib
from email.mime.text import MIMEText
m = MIMEText('test')
m['Subject'] = 'narve.ai SMTP test'
m['From'] = os.environ['SMTP_USER']
m['To'] = 'shocakarel@gmail.com'
with smtplib.SMTP(os.environ.get('SMTP_HOST','localhost'), int(os.environ.get('SMTP_PORT','587'))) as s:
    s.starttls()
    s.login(os.environ['SMTP_USER'], os.environ['SMTP_PASS'])
    s.sendmail(os.environ['SMTP_USER'], ['shocakarel@gmail.com'], m.as_string())
print('sent')
\""
```

## Monitoring

Cloudflare Health Checks poll `/health` every 60s (prod) and 5min (staging).
See `CLOUDFLARE_CHANGES.md` for setup instructions.

### Manual health check

```bash
# Full detail
curl -s https://narve.ai/health | python3 -m json.tool

# Just the status string
curl -s https://narve.ai/health | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])'
```

### What the status values mean

| Status | HTTP | Meaning |
|---|---|---|
| `ok` | 200 | Every check passed — app is fully functional |
| `degraded` | 200 | Non-critical check failed, app still works (e.g. encryption key missing in dev) |
| `error` | 503 | Critical dependency down (database unreachable, gate token missing in prod) |

If Cloudflare dashboard is unavailable, check each downstream directly:

```bash
# Tailscale should be reachable
ping -c 1 100.69.44.108

# SSH should work
ssh julianhabbig@100.69.44.108 'hostname'

# Tunnel should be connected
ssh julianhabbig@100.69.44.108 'systemctl is-active cloudflared'

# Uvicorn should be listening on 7000
ssh julianhabbig@100.69.44.108 'ss -tlnp | grep 7000'

# Health from the server itself (bypasses Cloudflare)
ssh julianhabbig@100.69.44.108 'curl -s http://127.0.0.1:7000/health | python3 -m json.tool'
```

## Emergency procedures

### Site is down

1. Check Cloudflare status: https://www.cloudflarestatus.com
2. Can you reach the server? `ping -c 1 100.69.44.108`
3. Is uvicorn running? `ssh julianhabbig@100.69.44.108 'pgrep -af uvicorn'`
4. Is the tunnel running? `ssh julianhabbig@100.69.44.108 'systemctl is-active cloudflared'`
5. Local health? `ssh julianhabbig@100.69.44.108 'curl -s localhost:7000/health'`
6. Logs: `ssh julianhabbig@100.69.44.108 'tail -100 /tmp/gateway.log'`
7. Restart uvicorn (see **Start the gateway**)
8. Restart tunnel if needed: `sudo systemctl restart cloudflared`
9. Last resort: `bash scripts/rollback.sh` to the last known-good commit

### Database is unavailable

SQLite can't really be "unavailable" unless the disk is full or the file is
corrupted. Check:

```bash
# Disk space
ssh julianhabbig@100.69.44.108 'df -h ~'

# File integrity
ssh julianhabbig@100.69.44.108 'cd ~/Habbig/gateway && python3 -c "
import sqlite3
c = sqlite3.connect(\"auth.db\")
print(c.execute(\"PRAGMA integrity_check\").fetchone())
"'
```

If the integrity check fails: restore from the most recent `auth.db.backup_*`
file (see **Database → Backup**).

### Cloudflare Tunnel is down

```bash
ssh julianhabbig@100.69.44.108 "
    sudo systemctl restart cloudflared
    sleep 3
    systemctl is-active cloudflared
    curl -s http://127.0.0.1:7000/health  # verify app still up
"
```

### Deploy broke production

```bash
bash scripts/rollback.sh
```

### Too many failed logins from one IP

The gateway has built-in account lockout (`_LOCKOUT_THRESHOLD = 5`) and IP
rate limiting. For sustained attacks, add a Cloudflare WAF rule (see
`CLOUDFLARE_CHANGES.md`). To release a locked account manually:

```bash
ssh julianhabbig@100.69.44.108 "cd ~/Habbig/gateway && python3 -c \"
import sqlite3
# Account lockouts are in-memory only, not SQL. Restart uvicorn to clear.
print('restart uvicorn to clear in-memory lockouts')
\""
```

## Contacts

| Role | Name | Contact |
|---|---|---|
| Owner | Julian | julian.habbig@icloud.com |
| Dev | Sho | shocakarel@gmail.com |
| Hosting | Tailscale (VPN), host VM self-managed | |
| DNS | Cloudflare | https://dash.cloudflare.com |
| Tunnel | Cloudflare Zero Trust | https://one.dash.cloudflare.com |
| Email SMTP | TBD | |
| Stripe | TBD (not yet integrated) | |

## GitHub Actions secrets

Set these under **Settings → Secrets and variables → Actions → New repository
secret**. They're referenced by `.github/workflows/deploy-*.yml`.

| Secret | Value |
|---|---|
| `STAGING_SSH_HOST` | Tailscale IP or hostname (e.g. `100.69.44.108`) |
| `STAGING_SSH_USER` | `julianhabbig` |
| `STAGING_SSH_KEY` | Private key whose public half is in `~/.ssh/authorized_keys` |
| `STAGING_SSH_PORT` | (optional) SSH port, default `22` |
| `PROD_SSH_HOST` | Same as staging (single host) |
| `PROD_SSH_USER` | `julianhabbig` |
| `PROD_SSH_KEY` | Private key |
| `PROD_SSH_PORT` | (optional) |

GitHub Actions runners live on public GitHub IPs. If the production host
is only reachable over Tailscale, add a Tailscale GitHub Action step before
the SSH step, or run a self-hosted runner on a Tailscale-connected machine.
