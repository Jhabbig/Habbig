# gateway/ — narve.ai central entry point

The single FastAPI process that fronts every dashboard. Port **7000**. Reverse proxy + apex auth/billing pages, all in one process. See [README.md](README.md) for the full architecture, deploy steps, and partner-access patterns.

## Don't touch unless you mean to

- **`config.json` top-level keys** (`crypto`, `weather`, `top_traders`, etc.) are tied to the `subscriptions` SQLite table. Renaming a key orphans every existing subscription. The `subdomain` value inside is renameable.
- **Cookie name `pm_gateway_session`** is scoped to `.narve.ai`. Renaming breaks all existing sessions.
- **`X-Gateway-User-Id` / `X-Gateway-User-Email` headers** — every dashboard trusts these. Don't change the header names without coordinating across all dashboards.
- **Localhost dev bypass** in `server.py` — auto-creates a `dev@local` user with all subs active. **Disabled when `PRODUCTION=1`.** Do not weaken the production gate.
- **`auth.db`** is SQLite, WAL mode, gitignored. Never commit it. Pre-existing `gateway/auth.db` here is a dev DB; production has its own.

## Stack

- FastAPI + uvicorn + httpx (reverse proxy + WebSocket forwarding)
- SQLite via stdlib `sqlite3`, PBKDF2-SHA256 password hashing, Fernet for at-rest secrets
- Redis (optional, for `cache.py` + `sse.py` + `poller.py`)
- Cloudflare Tunnel for public ingress (`setup_cloudflare.sh`)

## Files worth knowing

| File | What it is |
|---|---|
| `server.py` | The whole app — routes, proxy, auth middleware, sub gating, WebSocket forwarding |
| `db.py` | Schema + queries for users / sessions / subscriptions; **all DB access goes through this** |
| `config.json` | Runtime config — dashboards map, subdomain → port → price |
| `cache.py` | Redis caching + pub/sub layer (TTL'd dashboard API responses) |
| `poller.py` | Background fetcher that warms `cache.py` and publishes `data_updated` events |
| `sse.py` | EventSource stream — `/api/stream?dashboards=...` |
| `polymarket_client.py` | **Canonical** Polymarket Gamma + CLOB wrapper. All dashboards should call this (or gateway endpoints that use it), not roll their own |
| `kalshi_client.py` | Same idea for Kalshi |
| `alpaca_client.py` | Same idea for Alpaca |
| `mark_to_market.py` | PnL calculation across positions |
| `metrics.py` | Aggregated metrics endpoint |
| `email_relay/` | SMTP relay for invite tokens |
| `setup_cloudflare.sh` | Bulk-registers DNS routes for apex + every subdomain |
| `DEPLOY_NARVE.md` | **Step-by-step production deploy checklist** — read first when shipping |

## Conventions

- All DB writes go through `db.py`. Don't `sqlite3.connect()` ad hoc.
- All Polymarket/Kalshi/Alpaca calls go through `polymarket_client.py` / `kalshi_client.py` / `alpaca_client.py`. Don't write a new HTTP wrapper.
- Auth middleware sits in `server.py` — never add a parallel auth path.
- Subdomain routing is by `Host` header. The reverse proxy strips/rewrites the host downstream.

## Common gotchas

- **macOS AirPlay Receiver squats port 7000.** Locally, run with `GATEWAY_PORT=7001 ./start_dashboards.sh start` or disable AirPlay Receiver in System Settings → General → AirDrop & Handoff.
- **`PRODUCTION` env var** must be `1` (or `true`) on the live server. Otherwise dev bypass is on and anyone gets `dev@local` with full sub access.
- **`GATEWAY_COOKIE_SECRET`** should be 32+ random hex bytes (`openssl rand -hex 32`) on prod.
- **Localhost `*.localhost` subdomain test** — modern browsers auto-resolve, no `/etc/hosts` edit. But curl with `-H "Host:"` won't track cookies — use the browser for stateful flows.
- **Adding a new dashboard** — three places to update: `config.json` (the entry), `start_dashboards.sh` (launch line), `cloudflared tunnel route dns` (DNS). Plus restart cloudflared.

## Stripe wiring (later)

Placeholder lives in `server.py` → `billing_action()`. Replace `db.upsert_subscription(...)` with a Stripe Checkout Session create call; on webhook, call the same `upsert_subscription` with the returned `stripe_sub_id`. Schema column already exists.
