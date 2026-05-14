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

## Subproduct ports

Every subproduct is a self-contained FastAPI app behind the gateway.
Gateway proxies the matching subdomain to `127.0.0.1:<port>` on the
prod host; each subproduct ships its own `server.py` (and, where
applicable, its own SQLite store under the subproduct directory).

| Subproduct | Subdomain | Port | Directory | Status |
| --- | --- | --- | --- | --- |
| Voters Atlas | `voters.narve.ai` | `7051` | `voters-dashboard/` | live |
| Climate Change | `climate.narve.ai` | `7052` | `climate-dashboard/` | live |
| World Health | `world-health.narve.ai` | `7053` | `world-health-dashboard/` | skeleton |
| Eco Disasters | `disasters.narve.ai` | `7060` | `disasters-dashboard/` | live |
| Central Bank Tracker | `centralbank.narve.ai` | `7061` | `centralbank-dashboard/` | skeleton |
| Whale Watch | `whale.narve.ai` | `8053` | `whale-dashboard/` | skeleton |

Subdomain routing is configured on the Cloudflare Tunnel; the
tunnel forwards each hostname to `127.0.0.1:<port>` on
`100.69.44.108`. To verify a subproduct is up:

```bash
ssh julianhabbig@100.69.44.108 \
  "curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<port>/health"
```

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

### Deploy tarball — required paths

When packaging a full deploy bundle (e.g. fresh box, recovery, or
shipping subproducts in lock-step with the gateway), the tarball
MUST include:

```
gateway/                            # main app
gateway/static/fonts/GeistMono-Variable.woff2   # 71 KB — required asset
voters-dashboard/                   # port 7051
climate-dashboard/                  # port 7052
world-health-dashboard/             # port 7053
disasters-dashboard/                # port 7060
centralbank-dashboard/              # port 7061
whale-dashboard/                    # port 8053
```

If `GeistMono-Variable.woff2` is missing, monospace surfaces (code
blocks, tabular numbers, hashes, market IDs) silently regress to
`SF Mono` / `Menlo`. The fallback chain in `tokens.css` prevents a
hard failure but the visual identity drifts — treat the woff2 as a
deploy-blocking asset, not a nice-to-have.

Each subproduct directory needs its own venv install + uvicorn /
`python3 server.py` launch on its own port. Restarting the gateway
does NOT restart subproducts; each is its own process.

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

## Security headers

Canonical values applied on every gateway response (defined in
`gateway/server.py` ~ line 600, `SECURITY_HEADERS`). Pasted here in
full for grep-ability during incident response — when a browser
reports a feature blocked, search this file for the directive name
to confirm it is intentional.

```
Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=(), midi=(), magnetometer=(), gyroscope=(), accelerometer=(), ambient-light-sensor=(), autoplay=(), encrypted-media=(), fullscreen=(self), picture-in-picture=(), publickey-credentials-get=(self), sync-xhr=(), bluetooth=(), display-capture=(), serial=(), hid=(), clipboard-read=(), clipboard-write=(self), idle-detection=(), interest-cohort=(), browsing-topics=()

Cross-Origin-Resource-Policy: same-origin
Cross-Origin-Opener-Policy: same-origin
Referrer-Policy: strict-origin-when-cross-origin
X-XSS-Protection: 0
```

In production only, additionally:

```
Strict-Transport-Security: max-age=63072000; includeSubDomains; preload
```

Notable allow-listed features (everything else is `()` = deny):

| Feature | Allowance | Why |
| --- | --- | --- |
| `fullscreen` | `self` | Chart / image lightbox needs it on narve origin |
| `clipboard-write` | `self` | Copy-to-clipboard buttons on takes / share dialogs |
| `publickey-credentials-get` | `self` | WebAuthn / passkey login future-proofing |

To add a new feature: edit `SECURITY_HEADERS["Permissions-Policy"]`
in `gateway/server.py` and the matching row in the table above.
Never silently widen — the row in this table is the audit trail.

CSP is defined separately (same file, `CSP = "; ".join([...])`)
and includes nonce-based `script-src` plus tight `connect-src` /
`img-src` / `frame-ancestors 'none'`.

## Deploy policy

The branch `feature/platform-build` has multiple active Claude sessions
committing in parallel. See [CONTRIBUTING.md](CONTRIBUTING.md#parallel-session-discipline)
for pull-before-commit + server-commit-after-deploy discipline. Pushing
to `origin` is gated on the operator's explicit approval per session.


## Severity + response SLA

| Severity | Definition | Acknowledge | Mitigate |
| --- | --- | --- | --- |
| **SEV-1** | Site fully down, users can't access anything, data-loss risk. | 15 min | 2 h |
| **SEV-2** | Major feature broken, partial outage, payment flow degraded. | 1 h | 8 h |
| **SEV-3** | Minor feature broken, UX affected, no data risk. | 24 h | 1 week |
| **SEV-4** | Cosmetic / metrics anomaly, no user impact. | best effort | best effort |

### How to triage when the pager fires

1. **Five-minute rule.** If you can't classify SEV-1 vs SEV-2 within
   five minutes of the alert, default to SEV-1. Erring high is free;
   erring low loses SLA.
2. **Scope.** "All users" = SEV-1. "Pro tier only" = SEV-2. "One
   vocal Twitter user" = probably SEV-3.
3. **Data loss.** Anything that can destroy user-visible state
   (predictions, saved items, subscriptions, portfolio connections)
   jumps one tier up on the table above.

### Playbooks

Specific incident recipes live in [`playbooks/`](playbooks/) at repo
root. Index:

* [site_down.md](playbooks/site_down.md) — SEV-1
* [database_corruption.md](playbooks/database_corruption.md) — SEV-1
* [admin_account_takeover.md](playbooks/admin_account_takeover.md) — SEV-1
* [cloudflare_incident.md](playbooks/cloudflare_incident.md) — SEV-1/2
* [stripe_webhook_flood.md](playbooks/stripe_webhook_flood.md) — SEV-2
* [mass_leak_detected.md](playbooks/mass_leak_detected.md) — SEV-2
* [runaway_claude_cost.md](playbooks/runaway_claude_cost.md) — SEV-3
* [scraper_falling_behind.md](playbooks/scraper_falling_behind.md) — SEV-3
* [suspicious_login_pattern.md](playbooks/suspicious_login_pattern.md) — SEV-3
* [postmortem_template.md](playbooks/postmortem_template.md) — fill in after any SEV-1/2
* [on_call.md](playbooks/on_call.md) — who carries the pager
