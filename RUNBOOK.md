# narve.ai Runbook

Operational runbook for production + staging. Deep-dive on subsystem
procedures lives in [gateway/RUNBOOK.md](gateway/RUNBOOK.md).

## Quick reference

| Thing | Value |
| --- | --- |
| Production URL | https://narve.ai |
| Staging URL | https://staging.narve.ai |
| Production `/health` | https://narve.ai/health |
| Server (Tailscale) | `100.69.44.108` |
| SSH user | `julianhabbig` |
| Project path on server | `~/Habbig/gateway` |
| Production port | `7000` |
| Staging port | `7001` |
| Production DB | `~/Habbig/gateway/auth.db` (SQLite, WAL) |
| Production env file | `~/.gateway_env` |
| Cloudflare Tunnel | `systemctl status cloudflared` |
| Prod log | `/tmp/gateway.log` |
| Branch | `feature/platform-build` |

## Deploy a change

Every file goes as a separate `scp` — never `rsync` with multiple source
args, it puts files in the wrong directories.

```bash
# 1. ship modified files one at a time
scp gateway/server.py julianhabbig@100.69.44.108:~/Habbig/gateway/server.py
scp gateway/static/market_detail.html \
    julianhabbig@100.69.44.108:~/Habbig/gateway/static/market_detail.html

# 2. restart uvicorn, preserving secrets from the old process
ssh julianhabbig@100.69.44.108 '
  PID=$(fuser 7000/tcp 2>/dev/null | awk "{print \$NF}")
  TOK=$(tr "\0" "\n" < /proc/$PID/environ | grep ^SITE_ACCESS_TOKEN= | cut -d= -f2-)
  SEC=$(tr "\0" "\n" < /proc/$PID/environ | grep ^GATEWAY_COOKIE_SECRET= | cut -d= -f2-)
  KEY=$(tr "\0" "\n" < /proc/$PID/environ | grep ^CREDENTIALS_ENCRYPTION_KEY= | cut -d= -f2-)
  fuser -k 7000/tcp; sleep 2
  cd ~/Habbig/gateway
  nohup env PRODUCTION=1 \
    SITE_ACCESS_TOKEN="$TOK" \
    GATEWAY_COOKIE_SECRET="$SEC" \
    CREDENTIALS_ENCRYPTION_KEY="$KEY" \
    EMAIL_DRY_RUN=true EMAIL_FROM=noreply@narve.ai APP_URL=https://narve.ai \
    python3 -m uvicorn server:app --host 127.0.0.1 --port 7000 \
    > /tmp/gateway.log 2>&1 &
'

# 3. verify
curl -sS -o /dev/null -w "%{http_code}\n" http://100.69.44.108:7000/health

# 4. MUST commit on server (the server git repo has the OLD files committed;
#    any git op on the server reverts your changes otherwise)
ssh julianhabbig@100.69.44.108 "cd ~/Habbig/gateway && git add -A && git commit -m 'deploy: <summary>'"
```

## Rollback

```bash
ssh julianhabbig@100.69.44.108
cd ~/Habbig/gateway
git log --oneline -5                     # find previous good SHA
git reset --hard <SHA>
fuser -k 7000/tcp; sleep 2
# re-launch uvicorn (see Deploy step 2)
curl http://127.0.0.1:7000/health
```

## Port 7000 is taken

A legacy Polymarket gateway `systemd` service may grab 7000 after reboot:

```bash
sudo systemctl disable --now polymarket-gateway   # if the unit exists
fuser -k 7000/tcp
```

## Run migrations

Migrations auto-run at startup (`upgrade_to_head()` in `server.py`).
Manual invocation:

```bash
cd ~/Habbig/gateway
python3 -c "import migrations; migrations.upgrade_to_head()"
```

Migration files live at `gateway/migrations/NNN_slug.py`. Current head is
`130_feedback.py`. See [CONTRIBUTING.md](CONTRIBUTING.md#migration-rules)
for numbering discipline.

## Cache-bust CSS / JS

Cloudflare caches HTML. After a CSS or JS change:

- Bump `?v=N` in every `<link rel="stylesheet">` and `<script src="...">`
  that references it.
- Or purge via the Cloudflare dashboard / MCP tool.

## Common issues

| Symptom | Cause | Fix |
| --- | --- | --- |
| `sqlite3.Row` has no attribute `get` | Calling `row.get("key")` | Use `row["key"]`; Row has no dict-get |
| `{{ key }}` literal in rendered HTML | `render_page` context missing `key` or HTML value not prefixed | Pass `key=...` or use `raw_key=...` for HTML content |
| Startup: `GATEWAY_COOKIE_SECRET must be set in production` | Missing env when relaunching | Pull it from the old pid's `/proc/<pid>/environ` before killing |
| Email is dry-run only | `EMAIL_DRY_RUN=true` or no relay configured | Unset DRY_RUN and set `EMAIL_RELAY_URL` or `SMTP_HOST` in the env file |
| Migration crashes at startup | Bad `revision` / `down_revision` chain | See [CONTRIBUTING.md](CONTRIBUTING.md#migration-rules) |

## Incident response

1. Check `/status` — any component red?
2. Check `/tmp/gateway.log` on server — recent exceptions?
   ```
   ssh julianhabbig@100.69.44.108 "tail -200 /tmp/gateway.log | grep -iE 'error|fatal'"
   ```
3. Check `/admin/jobs` — any failed recent runs?
4. Check `/admin/performance` — slow queries spiking?
5. If DB is locked:
   ```
   sqlite3 ~/Habbig/gateway/auth.db ".backup /tmp/recovery.db"
   # investigate outside the live DB
   ```
6. If everything is on fire, gate the site to invite-only and communicate
   on `/status`:
   ```
   # on server, export a short site-access token and restart
   ```

## Deploy policy

The branch `feature/platform-build` has multiple active Claude sessions
committing in parallel. See [CONTRIBUTING.md](CONTRIBUTING.md#parallel-session-discipline)
for pull-before-commit + server-commit-after-deploy discipline. Pushing
to `origin` is gated on the operator's explicit approval per session.
