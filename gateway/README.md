# Polymarket Gateway — narve.ai

Central entry point for every dashboard. One apex domain for login/signup/billing,
one subdomain per dashboard, one session cookie shared across all of them,
per-dashboard subscriptions so users can pick and mix.

## Domain layout

```
narve.ai                  → apex: login, signup, my dashboards, billing
crypto.narve.ai           → proxy → :8000 (crypto-dashboard)
markets.narve.ai          → proxy → :8050 (stock-dashboard)
midterm.narve.ai          → proxy → :8051 (midterm-dashboard)
traders.narve.ai          → proxy → :8052 (top-traders-dashboard)
weather.narve.ai          → proxy → :5050 (polymarket_weather_dashboard)
sports.narve.ai           → proxy → :8888 (sports-dashboard)
world.narve.ai            → proxy → :7050 (world-state-dashboard)
```

Change any subdomain name in `config.json`. The internal `key` stays the same
(it's how the subscriptions table identifies each dashboard).

## How it works

- **One cookie, all subdomains.** Session cookie `pm_gateway_session` is scoped
  to `.narve.ai` so logging in at the apex covers every subdomain.
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

**Python**
| File | Purpose |
|---|---|
| `server.py` | FastAPI app — apex routes, reverse proxy, WebSocket proxy, auth middleware, subscription gating. |
| `db.py` | SQLite layer — users, sessions, subscriptions, PBKDF2 password hashing, Fernet-encrypted secrets. |
| `cache.py` | Redis caching + pub/sub layer. Caches dashboard API responses with TTL and publishes `data_updated` events to drive SSE. |
| `poller.py` | Background poller that fetches each dashboard's main API endpoints, stores responses in Redis, and publishes `data_updated` so SSE clients refresh instantly. |
| `sse.py` | Server-Sent Events stream — subscribes to Redis pub/sub and forwards events over `EventSource("/api/stream?dashboards=...")`. |

**Config / data**
| File | Purpose |
|---|---|
| `config.json` | **Edit this** to change subdomain names, prices, target ports, or accent colors per dashboard. The internal `key` for each dashboard must NOT change. |
| `auth.db` | SQLite database — users, sessions, subscriptions. Created automatically on first run. |
| `static/` | Apex pages and shared assets. See `static/README.md`. |

**Build / deploy**
| File | Purpose |
|---|---|
| `Dockerfile` | Container build for the `gateway` service. |
| `.dockerignore` | Excludes `auth.db`, `__pycache__`, etc. from the Docker build context. |
| `requirements.txt` | Python deps for the gateway process. |
| `setup_cloudflare.sh` | Runs all the `cloudflared tunnel route dns` commands for the apex + every subdomain in one go. |
| `DEPLOY_NARVE.md` | **Step-by-step checklist** for getting narve.ai online in production. |
| `.env.example` | Reference for env vars. Copy to `.env` to use. |
| `README.md` | This file. |

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

See **`DEPLOY_NARVE.md`** for the full step-by-step checklist. Short version:

1. Buy `narve.ai` (Cloudflare Registrar is cheapest; if elsewhere, change
   nameservers to Cloudflare).
2. Install & authenticate `cloudflared`.
3. Create a tunnel: `cloudflared tunnel create narve-gateway`.
4. Drop the ingress file at `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: <tunnel-id>
   credentials-file: /home/<you>/.cloudflared/<tunnel-id>.json
   ingress:
     - hostname: "*.narve.ai"
       service: http://localhost:7000
     - hostname: "narve.ai"
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
   cloudflared tunnel run narve-gateway
   ```
7. Visit `https://narve.ai` — real signup, real subscription gating, dev
   bypass automatically disabled.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `PRODUCTION` | unset | When set to `1`/`true`, disables the localhost dev bypass **and** flips session cookies to `secure=True` (requires HTTPS). **Always set this on the live server.** |
| `GATEWAY_COOKIE_SECRET` | unset | Reserved for future signed-cookie use. Currently only checked for presence in production startup logs. Recommended: `openssl rand -hex 32`. |

## Adding a new dashboard

1. Add its entry to `config.json` with a unique top-level key, subdomain,
   target port, display name, accent color, and prices.
2. Add a startup line to `start_dashboards.sh`.
3. Run `cloudflared tunnel route dns <tunnel-id> <newsub>.narve.ai`.
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
   git commit -m "Initial import of Narve stack"
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
   `https://narve.ai/signup`, then running (on Ubuntu):
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

