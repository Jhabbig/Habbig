# Architecture

narve.ai is a prediction-market intelligence platform. A single FastAPI
gateway at `gateway/` (port 7000) fronts thirteen subproduct services,
each on its own `*.narve.ai` subdomain and dedicated localhost port. All
processes run on one Ubuntu host behind Cloudflare; admin access is
Tailscale-only. Persistence is SQLite in WAL mode with FTS5 + JSON1.

This document is the canonical map of the platform — what runs where,
which module owns which data, and how a request travels from
Cloudflare's edge to a response.

---

## 1. Overview

```
                              ┌────────────────────────────────┐
                              │  Cloudflare (DNS + Tunnel)     │
                              │  TLS, WAF, rate-limit, caching │
                              └───────────────┬────────────────┘
                                              │ cloudflared
                                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│           Ubuntu host (Tailscale: 100.69.44.108) — single box       │
│                                                                     │
│   ┌──────────────────┐                                              │
│   │  gateway :7000   │ ◀── apex, www, api, admin, staging           │
│   │  FastAPI + ARQ   │     primary auth.db (SQLite WAL)             │
│   └────────┬─────────┘                                              │
│            │   HMAC-signed SSO proxy + per-subproduct gate          │
│            ▼                                                        │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │  13 subproduct services on localhost (private ports)        │   │
│   │  sports :8888  weather :5050  world :7050  crypto :8000     │   │
│   │  midterm :8051 traders :8052  voters :7051  climate :7052   │   │
│   │  disasters :7060  whale :8053  cb :7061  health :7053       │   │
│   │  love :7062                                                 │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│   ┌──────────────────┐    ┌──────────────────────────────────────┐  │
│   │  staging :7001   │    │  scraper worker (detached proc)      │  │
│   └──────────────────┘    │  Playwright → /internal/predictions  │  │
│                           └──────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

No managed Redis is required (in-memory TTL cache is the default; if
`REDIS_URL` is reachable the cache layer transparently upgrades). No
queue broker, no horizontal scale-out. Cross-process fan-out uses
SQLite polling; SSE is in-process per connected client. Every external
port is gated behind Cloudflare — direct-origin requests are 403'd by
`middleware/subproduct.py`.

---

## 2. Subproducts — ports, code paths, status

Each subproduct is registered in `gateway/subproduct.py` (`SUBPRODUCTS`
catalogue). Subscriptions live in the apex `subscriptions` table keyed
by `dashboard_key`; entitlement is enforced both at the gateway
(`subproduct_access.py`) and at the subproduct service (HMAC token
verification).

| Subdomain              | Port | Code path                  | Status        |
| ---------------------- | ---- | -------------------------- | ------------- |
| sports.narve.ai        | 8888 | `sports-dashboard/`        | live          |
| weather.narve.ai       | 5050 | `polymarket_weather_dashboard/` | live     |
| world.narve.ai         | 7050 | `world-state-dashboard/`   | live          |
| crypto.narve.ai        | 8000 | `crypto-dashboard/`        | live          |
| midterm.narve.ai       | 8051 | `midterm-dashboard/`       | live          |
| traders.narve.ai       | 8052 | `top-traders-dashboard/`   | live          |
| voters.narve.ai        | 7051 | `voters-dashboard/`        | live          |
| climate.narve.ai       | 7052 | `climate-dashboard/`       | live          |
| disasters.narve.ai     | 7060 | `disasters-dashboard/`     | live          |
| whale.narve.ai         | 8053 | `whale-dashboard/`         | MVP           |
| cb.narve.ai            | 7061 | `centralbank-dashboard/`   | MVP           |
| health.narve.ai        | 7053 | `world-health-dashboard/`  | MVP           |
| love.narve.ai          | 7062 | `love-dashboard/`          | MVP (new — scaffold only) |

Notes:
- `traders` is the only subdomain where `dashboard_key` ≠ `slug`
  (`dashboard_key = top_traders`). `DASHBOARD_KEY_FOR_SLUG` in
  `subproduct.py` bridges the two for the subscriptions table.
- `love-dashboard/` currently contains only `Dockerfile`, `requirements.txt`,
  and `data/`. The catalogue entry, port reservation, and Cloudflare
  routing land in the same release window as the first server.py commit.
- Each subproduct service shares the same auth-cookie domain via
  Cloudflare Tunnel, so a single `narve_session` works platform-wide.

---

## 3. Gateway anatomy

Everything user-facing on the apex (`narve.ai`, `www.`, `api.`,
`admin.`, `staging.`) is served by `gateway/server.py`. The recent
decomposition split SQL helpers out of `db.py` into `gateway/queries/`,
and middleware/auth/security/email/jobs into their own packages.

| Path                       | Lines | Role                                                                 |
| -------------------------- | ----- | -------------------------------------------------------------------- |
| `gateway/server.py`        | 7324  | FastAPI app, middleware chain, route registration, lifespan hooks    |
| `gateway/db.py`            | 1394  | `db.conn()` context manager + thin user/session/subscription helpers |
| `gateway/queries/`         | 21 modules | SQL helpers extracted from `db.py` — admin, auth, markets, sources, profile, subscriptions, sharing_metrics, predictions, claude_usage, performance, topics, watchlist, collections, embeds, environmental, intelligence, newsletter, onboarding, data_exports, query_tracer |
| `gateway/cache/`           | 3     | TTL + optional Redis-backed cache (`service.py`, `ttl.py`)           |
| `gateway/auth/`            | 4     | Cookies, guards, middleware, session hardening                       |
| `gateway/security/`        | 7     | CSRF, audit log, rate limiter, input hygiene, idempotency, timezones |
| `gateway/middleware/`      | 4     | Request-lifecycle middleware (subproduct gate, perf, bulk-data RL)   |
| `gateway/email_system/`    | 5     | `EmailService` chokepoint, renderer, unsubscribe, templates/         |
| `gateway/jobs/`            | 30 modules | ARQ background jobs + in-process fallback (registry, worker, …)  |
| `gateway/i18n/locales/`    | 4 locales × 262 keys | en / de / es / pt-br                                  |
| `gateway/migrations/`      | 94    | Numbered, idempotent, append-only — run on boot via `db.migrate()`   |

Selected route modules (each self-registers via `register(app)` or
top-level `@app.get`): `market_routes`, `take_routes`,
`user_prediction_routes`, `billing_routes`, `admin_routes`,
`intelligence_routes`, `subproduct_dashboard_routes`,
`subproduct_signup_routes`, `forecast_routes`, `portfolio_routes`,
`affiliate_routes`, `collections_routes`, `notification_routes`,
`scenarios_routes`, `embed_routes`, `feedback_routes`, `webhooks_routes`,
`api_v1`, `api_keys_routes`.

---

## 4. Request lifecycle and data flow

```
   request → Cloudflare → cloudflared tunnel → uvicorn :7000
       │
       ▼
   [SecurityHeadersMiddleware]   CSP, HSTS, X-Frame-Options, Referrer-Policy
       │
       ▼
   [SubproductMiddleware]        rejects direct-origin requests; resolves
       │                         host → slug via SUBPRODUCTS catalogue
       ▼
   [CSRFMiddleware]              double-submit token on POST/PUT/PATCH/DELETE
       │                         (webhook + public POSTs whitelisted)
       ▼
   [GateMiddleware]              pre-release access cookie (public allowlist)
       │
       ▼
   [LoggingContextMiddleware]    injects request_id, client_ip, user_id
       │                         into the structured-log context
       ▼
   route handler ──▶ db.conn() context  (begin/commit/rollback)
       │
       │ subproduct request? ──▶ HMAC-sign request, proxy to localhost:<port>
       │                         subproduct verifies signature + session,
       │                         renders HTML or JSON, gateway streams back
       │
       ▼
   response (middleware in reverse) → cloudflared → Cloudflare → client
```

Subproduct SSO: the gateway signs proxied requests with a per-subproduct
HMAC shared secret + short TTL nonce. Subproduct services trust nothing
that doesn't carry a valid signature, so direct-port traffic (even on
the same host) is rejected. Cookies are scoped to `.narve.ai` so the
`narve_session` works across all subdomains.

Auth flow (token-first): `/token` → `/auth/validate-token` (sets short
`pending_token` HMAC cookie) → `/register` or `/login` → long-lived
`narve_session` (7d, HttpOnly, SHA-256 hashed at rest, `MAX_SESSIONS_PER_USER = 3`).

Scrape → extract → score → present remains the upstream pipeline that
feeds the apex product:

```
  scraper → intelligence (Claude Haiku) → credibility (Bayesian, time-decay)
              ↓                                ↓
       predictions table                source_credibility
              ↓
       market_takes (community) + market_routes (presentation)
```

---

## 5. Third-party dependencies

| Service                 | Purpose                                                  |
| ----------------------- | -------------------------------------------------------- |
| Anthropic Claude        | Prediction extraction, classification, intelligence chat (Haiku for batch, Sonnet for chat) |
| Stripe                  | Subscription checkout — **test mode only**, no live charges yet |
| Sentry                  | Exception capture (`observability/sentry_setup.py`)     |
| BetterStack / Logtail   | Structured-log shipping (`logging_config.py`)            |
| Polymarket Gamma API    | Market price + metadata (`backend/markets/polymarket_client.py`) |
| Kalshi API              | Market price + metadata (`backend/markets/kalshi_client.py`)     |
| SEC EDGAR               | Insider trading + 13F filings (`insider/`, whale-dashboard) |
| FRED                    | Macro economic series                                    |
| ECB SDW                 | Euro-area macro data                                     |
| BoE database            | UK macro data                                            |
| WHO DON RSS             | Disease outbreak feed (world-health-dashboard)           |
| FDA Drug Shortages      | Pharma supply signals (world-health-dashboard)           |
| Cloudflare              | DNS, Tunnel, TLS termination, WAF, edge rate-limiting    |
| Tailscale               | Admin SSH only — never user-facing                       |

Redis is optional: `cache/service.py` upgrades to `redis.asyncio` when
`REDIS_URL` is reachable, otherwise falls back to in-process TTL.

---

## 6. What's NOT in scope yet

- **AWS migration** — gated on hitting 50+ paying users. Until then the
  single-box deployment is intentional and load-tested.
- **Stripe live mode** — `STRIPE_SECRET_KEY` is a `sk_test_…` key.
  Checkout works end-to-end against test cards; flipping to live mode
  is a one-config change, but not before the AWS migration.
- **2FA** — removed in migration `019_remove_2fa.py`. The intent is to
  re-introduce it later as WebAuthn / passkeys rather than TOTP.
- **Horizontal scale-out** — every subproduct shares one host and one
  SQLite file. No read replicas, no sharding, no cross-host queue.

---

## 7. Diagrams

ASCII art above (sections 1 and 4) is the authoritative shape today.
For the canonical visual diagrams — subproduct relationships, billing
state machine, scraper internals — see the [[narve.ai]] hub in Obsidian.

```
                  apex (narve.ai)
                       │
        ┌──────────────┼──────────────┐
        │              │              │
   subproducts    admin / api    auth + billing
   (13 dashboards) (gateway)     (gateway)
        │
   each on its own *.narve.ai subdomain,
   private localhost port, HMAC-gated proxy
```

---

## Subsystem boundaries (reference)

| Subsystem | Path | Owns |
| --- | --- | --- |
| HTTP / routing | `server.py` + `*_routes.py` | FastAPI app, middleware, request lifecycle |
| DB core | `db.py` + `queries/` | `db.conn()` context manager + extracted SQL helpers |
| Per-feature DB layers | `db_takes.py`, `db_collections.py`, `db_affiliate.py`, `db_forecasts.py`, `db_referrals.py`, `db_sharing.py` | Feature-isolated queries |
| AI | `ai/` + `intelligence/` | Claude client wrappers, prompt caching, extraction, backtester |
| Credibility | `credibility/` | Bayesian time-decay scoring, source_credibility writes |
| Scraper | `scraper/` | Detached worker. Playwright + Stealth for Twitter / TruthSocial; HTTP for Metaculus + Substack |
| Markets | `backend/markets/` | Polymarket + Kalshi clients, unified `Market` shape |
| Portfolio | `portfolio/` | Live positions both exchanges, encrypted creds (migration 017) |
| Jobs | `jobs/` | ARQ with in-process fallback — resolution polling, email send, credibility recompute, forecast sync, weekly digest, churn signals |
| Realtime | `realtime/` | WebSocket + SSE, one in-memory queue per connection |
| Auth | `auth/` | Cookie signing, session hardening, guards, token-first flow |
| Subproduct access | `subproduct*.py`, `middleware/subproduct.py` | Per-dashboard entitlement gate, direct-origin rejection |
| Billing | `billing_routes.py`, `stripe_webhook_hardening.py` | Stripe Checkout, subscription state, gift flows |
| Email | `email_system/` | `EmailService` chokepoint — dry-run / MailChannels / SMTP transports |
| Forensics | `forensics/` | Per-user response watermarking + leak attribution |
| Admin | `admin_routes.py` + `impersonation.py` + `security/audit.py` | Admin panel, impersonation with blocked destructive paths, append-only audit log |
| Intelligence chat | `intelligence_routes.py` | Paid Claude-backed chat over every user-visible fact |

---

## Observability

- **Structured logs** via `logging_config.py` — every record carries
  `request_id`, `user_id`, `service`, `environment`. Shipped to BetterStack/Logtail.
- **Sentry** for exceptions (`observability/sentry_setup.py`).
- **`/health`** — liveness check, no DB touches.
- **`/admin/performance`** — slow-query log, request-count rollups.
- **`take_resolution_runs`** table — audit log for the daily take resolver.
- **Forensic signing** — every list-shaped API response is signed with
  the viewer's per-user seed so leaks are attributable via
  `forensics/extract_watermark.py`.
