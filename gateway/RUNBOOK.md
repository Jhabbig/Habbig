# narve.ai Operations Runbook

Last updated: 2026-05-15

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

**Use `setsid` + `nohup`.** Plain `nohup` alone dies when the ssh session
exits (the parent shell takes the process down with it). `setsid` puts
uvicorn in a new session detached from the controlling terminal so it
survives ssh disconnect. Confirmed 2026-05-15 — plain nohup repeatedly
died on disconnect; `setsid` form has been stable.

```bash
ssh julianhabbig@100.69.44.108
cd ~/Habbig
set -a; . ~/.gateway_env; set +a
pkill -f "uvicorn server:app.*7000" 2>&1 || true
sleep 2
setsid bash -c "nohup python3 -m uvicorn server:app \
    --host 127.0.0.1 --port 7000 --app-dir gateway \
    > /tmp/gateway.log 2>&1 &" < /dev/null
sleep 6
curl -s http://127.0.0.1:7000/health  # local check (subproduct mw will 403 — see note)
curl -s https://narve.ai/health        # real check via Cloudflare
```

Note `--app-dir gateway` lets you launch from `~/Habbig` instead of
`cd`ing into `gateway/` first. Either works; pick one and stick with it.

> ⚠️ The systemd unit `polymarket-gateway.service` is **masked** —
> don't try `systemctl start polymarket-gateway`. Production runs
> uvicorn via direct `nohup` + `setsid` only.

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

## Sentry (error reporting)

**Status:** code is wired up, DSN is not set, so `init_sentry()` returns
`False` and every Sentry call is a no-op. This is intentional — Sentry is
opt-in. The audit log (`NARVE_SECURITY_AUDIT.md`) tracks this as an
accepted LOW (configurable, awaiting user choice).

### What Sentry does for narve.ai when enabled

- **Error aggregation** — every uncaught exception in FastAPI handlers,
  background jobs, and the scraper is grouped, deduped, and shown with a
  stack trace + request context (sanitised — see Privacy below).
- **Release tracking** — `release=detect_release()` tags each event with
  the current git SHA so regressions are pinned to a specific deploy.
- **Performance traces** — 10% sampling of request spans + SQL queries
  via `SqlalchemyIntegration`. Slow endpoints surface as `p95` regressions
  on the Sentry performance dashboard.

### How to enable

1. Create a Sentry account at https://sentry.io (free tier is fine —
   5k errors/mo, 10k performance units/mo).
2. Create a new project, platform = **FastAPI**. Sentry will hand you a
   DSN like `https://<key>@o<org>.ingest.sentry.io/<project>`.
3. SSH to the prod box and append the DSN to the gateway env file:

   ```bash
   ssh julianhabbig@100.69.44.108
   echo 'SENTRY_DSN=https://<key>@o<org>.ingest.sentry.io/<project>' >> ~/.gateway_env
   # Optional tuning (defaults are sane):
   # echo 'SENTRY_TRACES_SAMPLE_RATE=0.1' >> ~/.gateway_env
   # echo 'SENTRY_PROFILES_SAMPLE_RATE=0.1' >> ~/.gateway_env
   # echo 'ENVIRONMENT=production' >> ~/.gateway_env   # already set
   ```

4. Restart uvicorn so the new env is loaded (`Restart uvicorn` section
   below). `init_sentry()` runs once at startup and is idempotent.
5. Verify: trigger a test error from the admin panel
   (`/admin/sentry/test`) and confirm it appears in the Sentry UI.
6. (Optional) Repeat steps 1–4 with `~/.gateway_env_staging` to wire up
   the staging environment to a separate Sentry project. Keep prod and
   staging on **different DSNs** so noisy staging errors don't pollute
   the prod issue list.

### Env vars read by the init code

Defined in `gateway/observability/sentry_setup.py:init_sentry`:

| Var | Default | Purpose |
|---|---|---|
| `SENTRY_DSN` | (unset → Sentry disabled) | Required to enable. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.1` | Fraction of requests traced. |
| `SENTRY_PROFILES_SAMPLE_RATE` | `0.1` | Fraction of traces profiled. |
| `ENVIRONMENT` | `production` | Tag on every event (`production`/`staging`). |

The scraper service has its own init in
`gateway/scraper/observability.py:49-78` and reads the same env vars.
Both call the shared `scrub_sensitive_data` hook.

### What to expect when enabled

- Every uncaught exception → Sentry (sanitised).
- WARNING-level log lines → Sentry breadcrumb.
- ERROR-level log lines → Sentry event.
- ~10% of requests recorded as performance traces.
- ~10% of those traces profiled (CPU sampling).
- Release SHA visible on each event for regression tracking.

### Privacy — PII scrubbing

Sentry's `before_send` hook is `scrub_sensitive_data` in
`gateway/observability/sentry_setup.py:23-55`. It runs on **every event**
before it leaves the server and:

- `[Filtered]`s any header in
  `{authorization, x-csrf-token, cookie, set-cookie}`.
- `[Filtered]`s **all** request cookies (no allowlist).
- `[Filtered]`s any request body / `extra` field whose key contains
  `password`, `token`, `secret`, `key`, `card`, `cvv`, `cvc`, `ssn`,
  `pin`, `credit`, `bank`, or `account_number`.
- `[Filtered]`s the full query string if any of the same hints appear.

User context is also minimised: `set_user_context` at
`sentry_setup.py:106-121` hashes the internal user id
(`sha256("narve:<id>")[:16]`) and **drops the email entirely** — only the
hashed id and tier tag are sent. `send_default_pii=False` is also set on
the SDK init so the Sentry SDK does not auto-attach IP / username.

**If you enable Sentry, audit the hint list once** — anything not on it
(e.g. a future "license_key" field) will be sent in cleartext until added.

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

## Safe deploy procedure (manual production deploys)

The automated scripts (`scripts/deploy-production.sh`,
`scripts/rollback.sh`) cover the common path. This section is the
belt-and-braces checklist for when you're deploying manually over ssh —
e.g. hotfixes, schema-changing migrations, or any deploy where you want
to keep an explicit rollback handle. Codified after the 2026-05-15 deploy
where divergent server history + pending migration nearly trashed
`auth.db`.

### Pre-flight on the server

```bash
ssh julianhabbig@100.69.44.108
cd ~/Habbig

# 1. Tag the current server HEAD as a rollback handle BEFORE doing anything
#    destructive (git reset, pull --rebase, etc.). Force-overwrite is fine
#    — the tag is local-only and only used to find your way back.
git tag -f "server-snapshot-pre-deploy-$(date +%Y%m%d-%H%M)" HEAD

# 2. If the server has uncommitted WIP you want to preserve before pulling,
#    stash it. Otherwise it'll get clobbered by git reset / checkout.
git status --short                # what's dirty?
git stash push -m "server pre-deploy snapshot $(date +%Y%m%d-%H%M)"

# 3. Check whether server has local commits not in origin (diverged history).
git log --oneline HEAD ^origin/feature/platform-build
#   → If non-empty, DON'T blindly reset. Read each local commit:
git log -p HEAD ^origin/feature/platform-build
#   Either cherry-pick them into a real PR, or — if confirmed stale —
#   reset after tagging:
git reset --hard origin/feature/platform-build
```

### Migration safety

Production has no separate migration framework — schema lives in
`db.init_db()`. **But** if you're running anything in `gateway/migrations/`
that mutates `auth.db`, take a timestamped snapshot first:

```bash
cp gateway/auth.db gateway/auth.db.backup-pre-MIGRATION_ID-$(date +%Y%m%d-%H%M)
# e.g. auth.db.backup-pre-105-20260515-2214
```

When systemd is not in use, run the migration head invocation directly:

```bash
cd ~/Habbig/gateway && python3 -c "import migrations; print(migrations.upgrade_to_head())"
```

If the migration fails or the app starts misbehaving, stop uvicorn, swap
the backup back in, and restart:

```bash
fuser -k 7000/tcp; sleep 2
mv gateway/auth.db gateway/auth.db.failed-$(date +%s)
cp gateway/auth.db.backup-pre-MIGRATION_ID-* gateway/auth.db
# then restart via the setsid command in "Start the gateway"
```

### Restart uvicorn (the proven incantation)

This is the same command as **Start the gateway (production)** but it's
worth repeating in the deploy flow because the `setsid` part is the
non-obvious piece. Without it, uvicorn dies the moment your ssh session
ends.

```bash
pkill -f "uvicorn server:app.*7000" 2>&1 || true
sleep 2
setsid bash -c "nohup python3 -m uvicorn server:app \
    --host 127.0.0.1 --port 7000 --app-dir gateway \
    > /tmp/gateway.log 2>&1 &" < /dev/null
sleep 6
```

### Verify the deploy

```bash
# Local check FROM THE SERVER will hit the subproduct middleware, which
# 403s requests with a localhost Host header in production. That's
# expected — it's not a deploy failure. Test via Cloudflare instead.
curl -s http://127.0.0.1:7000/health   # may 403 — DO NOT rely on this
curl -s https://narve.ai/health | python3 -m json.tool   # real check
```

If `/health` is 200 via `https://narve.ai` but 403 via
`http://127.0.0.1:7000`, the deploy is fine — the middleware is doing its
job.

### Stripe webhook smoke test

When `STRIPE_IP_ALLOWLIST_ENFORCE=true` (the production default), the
webhook endpoint will 403 every request not coming from the 12 published
Stripe CIDR ranges. Test from a non-Stripe IP to confirm enforcement is
on, **not** to verify a real webhook works — those have to come from
Stripe's own infra (use Stripe CLI `stripe trigger` against a test
account).

```bash
# Should return 403 from your laptop / a non-Stripe IP:
curl -s -o /dev/null -w "%{http_code}\n" https://narve.ai/stripe/webhook
```

### Rollback path

If the deploy turns out broken:

```bash
ssh julianhabbig@100.69.44.108
cd ~/Habbig
git tag --list "server-snapshot-pre-deploy-*" | tail -5
git reset --hard server-snapshot-pre-deploy-YYYYMMDD-HHMM
# If you snapshotted auth.db, restore it too:
cp gateway/auth.db.backup-pre-MIGRATION_ID-* gateway/auth.db
# Restart uvicorn via the setsid command.
```

Or, for the common case of "just go back one commit on origin":
`bash scripts/rollback.sh` from your laptop.

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
