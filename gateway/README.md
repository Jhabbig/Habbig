# Polymarket Gateway — habbig.com

Central entry point for every dashboard. One apex domain for login/signup/billing,
one subdomain per dashboard, one session cookie shared across all of them,
per-dashboard subscriptions so users can pick and mix.

## Domain layout

```
habbig.com                  → apex: login, signup, my dashboards, billing
crypto.habbig.com           → proxy → :8000 (crypto-dashboard)
markets.habbig.com          → proxy → :8050 (stock-dashboard)
midterm.habbig.com          → proxy → :8051 (midterm-dashboard)
traders.habbig.com          → proxy → :8052 (top-traders-dashboard)
weather.habbig.com          → proxy → :5050 (polymarket_weather_dashboard)
sports.habbig.com           → proxy → :8888 (sports-dashboard)
world.habbig.com            → proxy → :7050 (world-state-dashboard)
```

Change any subdomain name in `config.json`. The internal `key` stays the same
(it's how the subscriptions table identifies each dashboard).

## How it works

- **One cookie, all subdomains.** Session cookie `pm_gateway_session` is scoped
  to `.habbig.com` so logging in at the apex covers every subdomain.
- **Per-request subscription check.** Every proxied request verifies the user
  has an active subscription for that specific dashboard. No sub →
  redirect to `/billing?dashboard=<key>`.
- **Dashboards stay untouched.** Crypto and sports have their own internal login
  pages — they just go unused. The gateway forwards `X-Gateway-User-Id` and
  `X-Gateway-User-Email` headers downstream so the dashboards can trust the
  identity if they ever want to read it.
- **WebSocket support** for crypto and sports, flagged by `supports_websocket`
  in `config.json`.
- **Localhost dev bypass:** when the request host is `localhost` / `*.localhost`
  and `PRODUCTION` is unset, the gateway auto-creates a `dev@local` user with
  all 7 dashboards active so you can preview without signing up. This is
  **disabled automatically when `PRODUCTION=1`**.

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI app — apex routes, reverse proxy, WebSocket proxy, auth middleware |
| `db.py` | SQLite layer — users, sessions, subscriptions, PBKDF2 password hashing |
| `config.json` | **Edit this** to change subdomain names, prices, or target ports |
| `auth.db` | SQLite database — created automatically on first run |
| `static/` | Apex pages — login, signup, dashboards, billing, shared CSS (Notion/Wispr tokens) |
| `requirements.txt` | Python deps for the gateway process |
| `DEPLOY_HABBIG.md` | **Step-by-step checklist** for getting habbig.com online |
| `setup_cloudflare.sh` | Helper that runs all the `cloudflared tunnel route dns` commands in one go |

## Run locally (no domain needed)

```bash
pip install -r gateway/requirements.txt
./start_dashboards.sh restart          # boots all 7 dashboards + gateway
open http://localhost:7000             # auto-logs you in as dev@local
```

Every "Open →" button links directly to its dashboard's local port, so
click-through works without any DNS fiddling.

To test the real subdomain flow locally (no dev bypass) use `*.localhost`:

```
http://habbig.localhost:7000          # <- doesn't auto-login, acts like production apex
http://crypto.localhost:7000          # <- proxies to :8000 via the gateway
```

## Production deployment

See **`DEPLOY_HABBIG.md`** for the full step-by-step checklist. Short version:

1. Buy `habbig.com` (Cloudflare Registrar is cheapest; if elsewhere, change
   nameservers to Cloudflare).
2. Install & authenticate `cloudflared`.
3. Create a tunnel: `cloudflared tunnel create habbig-gateway`.
4. Drop the ingress file at `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: <tunnel-id>
   credentials-file: /home/<you>/.cloudflared/<tunnel-id>.json
   ingress:
     - hostname: "*.habbig.com"
       service: http://localhost:7000
     - hostname: "habbig.com"
       service: http://localhost:7000
     - service: http_status:404
   ```
5. Run `./gateway/setup_cloudflare.sh <tunnel-id>` to register DNS routes
   for the apex + all 7 subdomains at once.
6. Flip to production mode on the host:
   ```bash
   export PRODUCTION=1
   export GATEWAY_COOKIE_SECRET="$(openssl rand -hex 32)"
   ./start_dashboards.sh restart
   cloudflared tunnel run habbig-gateway
   ```
7. Visit `https://habbig.com` — real signup, real subscription gating, dev
   bypass automatically disabled.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `PRODUCTION` | unset | When set to `1`/`true`, disables the localhost dev bypass **and** flips session cookies to `secure=True` (requires HTTPS). **Always set this on the live server.** |
| `GATEWAY_COOKIE_SECRET` | unset | Reserved for future signed-cookie use. Currently only checked for presence in production startup logs. Recommended: `openssl rand -hex 32`. |
| `ENVIRONMENT` | `production` | Tag used by Sentry events to separate `production` / `staging` / `development`. |
| `APP_VERSION` | `1.0.0` | Release tag attached to Sentry events for source-map and regression tracking. |
| `LEGAL_EMAIL` | `legal@narve.ai` | Rendered on `/terms`. |
| `PRIVACY_EMAIL` | `privacy@narve.ai` | Rendered on `/privacy`. |
| `SUPPORT_EMAIL` | `support@narve.ai` | Rendered on `/terms`. |
| `FEEDBACK_EMAIL` | unset | If set, every new feedback submission logs a notification line addressed to this inbox. Wire to SMTP later. |
| `ANALYTICS_ENABLED` | `true` | Set to `false` to disable server-side event recording (page views still 200 but no DB writes). |
| `ANTHROPIC_API_KEY` | unset | Required for Signal Search **and** the Intelligence assistant. Pages render a graceful error message when missing. |
| `SENTRY_DSN` | unset | Backend Sentry DSN. **Keep secret.** When unset, Sentry init is a no-op. |
| `SENTRY_DSN_PUBLIC` | unset | Frontend Sentry DSN — use a separate Sentry project so a leaked public key cannot read backend errors. |
| `SENTRY_AUTH_TOKEN` | unset | Optional. Enables the "recent errors" widget on the admin **System Health** tab via the Sentry REST API. |
| `SENTRY_ORG` / `SENTRY_PROJECT` | unset | Required by the recent-errors widget — pass your Sentry org slug and the project slug for `narve-backend`. |
| `SENTRY_DASHBOARD_URL` | unset | Direct link rendered on the System Health tab. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.1` | Performance trace sample rate. |
| `SENTRY_PROFILES_SAMPLE_RATE` | `0.1` | Profiling sample rate. |

### Sentry setup (one-time)

1. Create an account at sentry.io.
2. Create **two** projects: `narve-backend` (Python/FastAPI) and `narve-frontend` (Browser JavaScript).
3. Copy the backend DSN into `SENTRY_DSN` and the frontend DSN into `SENTRY_DSN_PUBLIC`.
4. (Optional) Generate an auth token at sentry.io/settings/auth-tokens/ with `project:read` scope and set `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT` so the admin **System Health** tab can show recent errors inline.
5. Restart the gateway. The startup log line `Sentry initialised platform=backend` confirms it's wired up.

The same `SENTRY_DSN` can also be set on the scraper service — `scraper/main.py` calls `init_sentry(platform="scraper")` and tags every twitter/truthsocial run with `scraper_platform` so you can filter errors by source.

## Adding a new dashboard

1. Add its entry to `config.json` with a unique top-level key, subdomain,
   target port, display name, accent color, and prices.
2. Add a startup line to `start_dashboards.sh`.
3. Run `cloudflared tunnel route dns <tunnel-id> <newsub>.habbig.com`.
4. Restart gateway + `cloudflared`.

## Wiring real Stripe payments later

The placeholder checkout lives in `server.py` → `billing_action()`. Replace
the `db.upsert_subscription(...)` call with a Stripe Checkout Session creation,
then call the same `upsert_subscription` with the returned `stripe_sub_id`
from your webhook handler when payment confirms. The `subscriptions` table
already has a `stripe_sub_id` column.

## Backup

Pre-gateway state of the whole Polymarket folder is archived at
`../Polymarket_backup_20260404_pre_gateway/` (sibling directory, ~8.6GB).

## Partner / collaborator access

Two common setups depending on how much your partner needs to do.

### Option A — Code-level collaboration via GitHub (recommended)

Good for a partner who will edit code, designs, and UI in parallel.

1. Initialize a repo in the project root on your Mac:
   ```bash
   cd "/Users/julianhabbig/Claude Vibecoding /Polymarket"
   git init
   git add .
   git commit -m "Initial import of Habbig stack"
   ```
2. Create a **private** GitHub repo (no password or token typed in chat) and
   push. Invite your partner as a collaborator from the GitHub web UI.
3. On the Ubuntu laptop, clone the same repo into `~/Polymarket-src` (kept
   separate from the running `~/Polymarket` tree so you can pull without
   disturbing running services), then rsync from `~/Polymarket-src` to
   `~/Polymarket` when deploying:
   ```bash
   rsync -avc --exclude venv --exclude auth.db --exclude __pycache__ \
     ~/Polymarket-src/ ~/Polymarket/
   sudo systemctl restart polymarket-*.service
   ```
4. Your partner gets an admin account on the gateway by signing up at
   `https://habbig.com/signup`, then running (on Ubuntu):
   ```bash
   cd ~/Polymarket/gateway
   ~/Polymarket/venv/bin/python3 -c "
   import db
   with db.conn() as c:
       c.execute('UPDATE users SET is_admin=1 WHERE email=?', ('partner@example.com',))
   "
   ```
   Admins bypass all per-dashboard subscription checks.

### Option B — Direct SSH access to the Ubuntu box

Good if your partner only needs to SSH in and tweak things directly.

1. Have your partner generate an ed25519 key on their machine:
   ```bash
   ssh-keygen -t ed25519 -C "partner"
   cat ~/.ssh/id_ed25519.pub
   ```
2. Append their public key to `~/.ssh/authorized_keys` on the Ubuntu laptop
   (you paste it in — Claude never handles keys):
   ```bash
   echo '<their public key>' >> ~/.ssh/authorized_keys
   ```
3. Share the Tailscale/LAN address with them. They can then
   `ssh julianhabbig@100.69.44.108` and edit files under `~/Polymarket/`
   directly.

For anything beyond a trusted co-founder, prefer **Option A** — GitHub gives
you commit history, code review, and easy rollback. Use Option B only for
quick one-off admin tasks.

## Logging & observability (BetterStack Logtail)

All three services (`app`, `scraper`, `worker`) emit **structured JSON logs**
via the centralised `logging_config.py` module. Logs go to three places:

1. **BetterStack Logtail** (cloud — searchable, 30 day retention on the $25
   Starter plan, beautiful live tail)
2. **Local rotating files** under `./logs/{service}.log` (backup, tails with
   `tail -f` if BetterStack is down)
3. **In-process ring buffer** — the last ~500 records, served by the admin
   panel's "Logs" tab for quick debugging without leaving the site

Security events (CSRF failures, rate-limit hits, suspicious activity) are
also mirrored to `./logs/security.log` so they can be greped independently.

### One-time BetterStack setup

1. Create a free account at [betterstack.com/logtail](https://betterstack.com/logtail)
   (1 GB/month, 3-day retention — enough for dev and early launch).
   Upgrade to the $25 Starter plan for 5 GB/month and 30-day retention before
   the real launch.
2. Create **three Sources**, one per service:
   - `narve-app`
   - `narve-scraper`
   - `narve-worker`
3. Copy each Source Token into `.env`:
   ```bash
   LOGTAIL_TOKEN_APP=xxxxxxxxxxxxxxxxxxxxxxxx
   LOGTAIL_TOKEN_SCRAPER=xxxxxxxxxxxxxxxxxxxxxxxx
   LOGTAIL_TOKEN_WORKER=xxxxxxxxxxxxxxxxxxxxxxxx
   ```
4. Install the Python client:
   ```bash
   pip install logtail-python
   ```
5. Restart all three services. You should see fresh records in BetterStack
   within ~30 seconds. If nothing arrives, check:
   - The `SERVICE_NAME` env var matches the token suffix
     (`SERVICE_NAME=app` → `LOGTAIL_TOKEN_APP`)
   - `logtail-python` is importable (`python3 -c "import logtail"`)
   - The admin-panel **Logs** tab shows the "BetterStack connected" badge

### Alerts to configure in BetterStack

Create these five alerts after the sources are collecting logs:

| Alert                  | Trigger                                                   | Channel         |
| ---------------------- | --------------------------------------------------------- | --------------- |
| **Error spike**        | >10 ERROR logs in 5 minutes                               | email + SMS     |
| **Pipeline stale**     | No `"Pipeline completed"` log in 20 minutes               | email           |
| **Scraper failing**    | >3 `"Scrape failed"` in 1 hour                            | email           |
| **Auth attack**        | >20 `csrf_failure` or `rate_limit_hit` events in 5 min    | email + SMS     |
| **Worker job failure** | >5 failed jobs in 1 hour                                  | email           |

BetterStack supports querying by any structured field, e.g.
`level:ERROR AND service:scraper` or `event:csrf_failure AND ip:1.2.3.4`.

### Required env vars

```ini
# .env
SERVICE_NAME=app                    # set per service (app | scraper | worker)
ENVIRONMENT=production              # production | dev
LOG_LEVEL=INFO                      # DEBUG | INFO | WARNING | ERROR
LOGTAIL_TOKEN_APP=
LOGTAIL_TOKEN_SCRAPER=
LOGTAIL_TOKEN_WORKER=
# LOG_RING_CAPACITY=500             # optional — admin-panel ring size
```

`SERVICE_NAME` should be set by the systemd unit / docker-compose block for
each process — never share an env file that hard-codes `SERVICE_NAME=app`
between services, or the scraper's logs will land in the app source.

### Admin panel Logs tab

Admins can tail live logs from `/admin` → **Logs** tab without leaving the
site. The tab supports:

- **Live tail** — last 100 records, auto-refreshing every 5s
- **Errors (last 24h)** — ERROR-level records grouped by message similarity
- **Filters** — level, service, free-text search

The panel reads from the in-process ring buffer only — for deeper searches or
long retention, click the "View in BetterStack →" link (shown when a token
is configured).

### Never log these fields

The `StructuredFormatter` automatically redacts any `extra={}` key whose name
contains one of: `password`, `secret`, `token`, `key`, `authorization`,
`auth`, `cookie`, `session`, `jwt`, `bearer`, `card`, `cvv`, `cvc`, `ssn`,
`pin`, `api_key`. A small allow-list exists for benign ID-ish fields
(`token_id`, `request_id`, `user_id`). When in doubt, don't pass the value
at all — the redaction is a safety net, not a policy.

