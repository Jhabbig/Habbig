# Gateway — Claude notes

Central auth + reverse proxy. Sits in front of every dashboard. **This is the
highest-risk code in the repo** — bugs here break login, leak sessions, or
bypass billing for everyone.

## Stack

FastAPI (not Flask — the root README's table is approximate). SQLite for
`auth.db`. Redis for cache + pub/sub. Cloudflared tunnel in production.
Stripe for billing.

## Files that matter

- `server.py` — single ~200KB module: apex routes, reverse proxy, WebSocket
  proxy, auth middleware, subscription gating, Stripe webhooks. Almost any
  meaningful change lands here.
- `db.py` — users, sessions, subscriptions. PBKDF2 hashing, Fernet-encrypted
  secrets. **Never log or print rows from this layer.**
- `config.json` — subdomains, prices, target ports, accent colours.
  **The `key` for each dashboard MUST NOT change** — it's the foreign key
  the `subscriptions` table uses. Subdomain / display name / price are fine.
- `cache.py`, `poller.py`, `sse.py` — Redis cache + SSE fanout. Touch when
  adding a new pollable endpoint or a new event type.

## Hard rules

- **Ask before editing auth, session, or billing code.** That means
  anything in `server.py` under the auth middleware, the `/login`,
  `/signup`, `/billing`, `/stripe/webhook` routes, or anything in `db.py`.
- **`auth.db` is live data on the server.** Don't delete, reset, or commit
  it. Local dev creates its own.
- **Localhost dev bypass auto-logs you in as `dev@local`** with all
  subscriptions active. It's gated by `PRODUCTION` being unset. **Never add
  code paths that bypass auth when `PRODUCTION=1`.**
- **`X-Gateway-User-Id` / `X-Gateway-User-Email`** are the trusted identity
  forwarded downstream. Don't accept them from external requests — the
  middleware must strip them on ingress before re-injecting.
- **WebSocket-capable dashboards** are flagged by `supports_websocket` in
  `config.json` (currently crypto, sports). Don't proxy WS to dashboards
  that don't support it.

## Env vars

`PRODUCTION`, `GATEWAY_COOKIE_SECRET`, `GATEWAY_SSO_SECRET` (shared with
dashboards), `STRIPE_*`, `SMTP_*`. New vars go in `gateway/.env.example`
**and** the repo-root `.env.example`.

## Verifying changes

```bash
./start_dashboards.sh restart      # boots all dashboards + gateway
open http://localhost:7000         # auto-logs in as dev@local
```

To exercise the real subdomain flow without DNS, use `*.localhost`:
`http://habbig.localhost:7000` (acts as apex), `http://crypto.localhost:7000`
(proxies to :8000). `*.localhost` does **not** trigger the dev bypass.

For auth/billing changes, also test:
- logout → login → subscription check → proxied request
- expired/invalid session cookie
- subdomain with no active subscription → redirect to `/billing`

ruff catches `F821` only; auth bugs are integration bugs — run the flows.
