# Architecture

Single-process FastAPI app backed by one SQLite database, with a detached
scraper worker feeding it. This document explains how the pieces
interact — what crosses process boundaries, which modules own which data,
and how requests flow through the stack.

---

## Deployment topology

```
                                  ┌───────────────────────────────────┐
                                  │  Cloudflare (DNS + Tunnel)        │
                                  │  TLS termination, WAF, rate-limit │
                                  └──────────────┬────────────────────┘
                                                 │ cloudflared
                                                 ▼
┌────────────────────────────────────────────────────────────────────────┐
│                       Ubuntu host (100.69.44.108)                      │
│                       Tailscale SSH for admin                          │
│                                                                        │
│   ┌──────────────────┐      ┌──────────────────────────────────────┐  │
│   │ uvicorn :7000    │─────▶│ auth.db (SQLite WAL, FTS5, JSON1)    │  │
│   │  gateway/server  │      │   users, sessions, predictions,      │  │
│   │  + ARQ in-proc   │      │   market_takes, subscriptions, ...   │  │
│   └────────┬─────────┘      └──────────────────────────────────────┘  │
│            │                                                           │
│            │ HTTP (batched)                                            │
│            ▼                                                           │
│   ┌──────────────────┐                                                 │
│   │ scraper worker   │  — Playwright-based social-media ingestion,     │
│   │  (separate proc) │    ships predictions to the gateway via         │
│   └──────────────────┘    /internal/predictions/bulk every ~15 min     │
│                                                                        │
│   ┌──────────────────┐                                                 │
│   │ uvicorn :7001    │   Staging mirror on the same host,              │
│   │  staging gateway │   separate auth-staging.db                      │
│   └──────────────────┘                                                 │
└────────────────────────────────────────────────────────────────────────┘
```

Everything runs on one machine. There is no Redis, no queue broker, no
horizontal scale-out. Cross-process fan-out uses SQLite polling; SSE is
in-process only (single queue per connected client).

---

## Data flow: scrape → extract → score → present

```
  ┌───────────────┐   ┌─────────────────┐   ┌──────────────────┐
  │ scraper/      │──▶│ intelligence/   │──▶│ credibility/     │
  │  Twitter      │   │ prediction      │   │  Bayesian        │
  │  Metaculus    │   │ extractor       │   │  time-decay      │
  │  Substack     │   │ (Claude Haiku)  │   │  scoring         │
  │  TruthSocial  │   │                 │   │                  │
  └───────────────┘   └─────────────────┘   └──────────────────┘
         │                      │                    │
         │                      │                    │
         ▼                      ▼                    ▼
    social_posts          predictions          source_credibility
    (raw text)          (direction + prob)    (global + per-category)

         ┌──────────────────┐
         │ markets (cached) │◀──  backend/markets/polymarket_client
         │  poly: / kalshi: │◀──  backend/markets/kalshi_client
         └──────────────────┘

                │
                ▼
         ┌──────────────────────────────────────────────┐
         │ presentation layer                           │
         │   /markets/{slug}         ← market_routes    │
         │   /api/v1/markets/{slug}/takes ← take_routes │
         │   /predictions/{id}       ← user_prediction  │
         │   /sources/{handle}       ← public profile   │
         │   /dashboards             ← paid grid view   │
         └──────────────────────────────────────────────┘
```

Key invariant: **predictions carry `market_id` (poly:... / kalshi:...)**,
and every downstream surface joins on that same key. `market_takes` uses
`market_slug` (same format) so takes, predictions, and votes all share
the market identity.

---

## Subsystem boundaries

| Subsystem | Path | Owns |
| --- | --- | --- |
| **HTTP / routing** | `server.py` + `*_routes.py` | FastAPI app, middleware, request lifecycle. Each `*_routes.py` self-registers via `register(app)` or top-level `@app.get`. |
| **DB core** | `db.py` | User / session / subscription / invite-token CRUD. `db.conn()` context manager. |
| **Per-feature DB layers** | `db_takes.py`, `db_collections.py`, `db_affiliate.py`, `db_forecasts.py`, ... | Feature-specific queries isolated from `db.py`. |
| **Queries** | `queries/` | Read-only query functions shared across routes (e.g. leaderboard, sharing metrics). |
| **AI** | `ai/` + `intelligence/` | Claude client wrappers, prompt caching, prediction extraction, backtester, retrospective. |
| **Credibility engine** | `credibility/` | Bayesian time-decay scoring. Writes `source_credibility` + `source_category_credibility`. |
| **Scraper** | `scraper/` | Detached worker. Playwright + Stealth for Twitter / TruthSocial; HTTP for Metaculus + Substack. |
| **Markets** | `backend/markets/` | Polymarket + Kalshi API clients. Unified `Market` shape. |
| **Portfolio** | `portfolio/` | User's live positions from both exchanges. Encrypted credentials per migration 017. |
| **Jobs** | `jobs/` | Background tasks: resolution polling, email send, credibility recompute, take resolution, forecast sync. ARQ with in-process fallback. |
| **Realtime** | `realtime/` | WebSocket + SSE. One in-memory subscriber queue per connection. |
| **Auth** | `auth/` | Cookie signing, session hardening, guards. Token-first flow (`/token` → register or login → `/dashboards`). |
| **Subproduct access** | `subproduct*.py`, `middleware/subproduct.py` | Per-dashboard entitlement gate. Rejects direct-origin requests to non-Cloudflare endpoints. |
| **Billing** | `billing_routes.py`, `stripe_webhook_hardening.py` | Stripe Checkout, subscription state, gift flows. |
| **Email** | `email_system/` | `EmailService` — the single chokepoint. Transports: dry-run / MailChannels relay / plain SMTP. |
| **Forensics** | `forensics/` | Per-user response watermarking + leak attribution on the admin side. |
| **Admin** | `admin_routes.py` + `impersonation.py` + `security/audit.py` | Admin panel, impersonation with blocked destructive paths, append-only audit log. |
| **Intelligence chat** | `intelligence_routes.py` | Paid Claude-backed chat with access to every user-visible fact. |

---

## Auth flow (token-first)

```
   ┌──────────┐  enter 32-char  ┌──────────────┐
   │  /token  │────────────────▶│ /auth/       │
   │          │   invite token  │  validate-   │
   └──────────┘                 │  token       │
                                └──────┬───────┘
                                       │ sets pending_token cookie (30m HMAC)
                     ┌─────────────────┼──────────────────┐
                     │  unclaimed?     │  claimed?        │
                     ▼                 │  (filter drops)  │
               ┌──────────┐            │                  │
               │/register │            ▼                  │
               │  + POST  │       (valid:false →          │
               │  /auth/  │        user must go to        │
               │  register│        /login directly)       │
               └────┬─────┘                               │
                    │                                     │
                    ▼                                     ▼
           ┌─────────────────────────────────────────────────┐
           │  /dashboards (paid grid) or /onboarding         │
           │  narve_session cookie (7d, HttpOnly, SHA-256    │
           │     hashed at rest in user_sessions)            │
           └─────────────────────────────────────────────────┘
```

Implementation notes:

- `pending_token` is a short-lived signed cookie so the `/register` +
  `/login` pages can deep-link into the flow without re-sending the raw
  token over the wire.
- `narve_session` is the long-lived session cookie. The raw token is
  SHA-256 hashed before persisting in `user_sessions` (migration 007 —
  "session hardening").
- `MAX_SESSIONS_PER_USER = 3` — the oldest session is revoked on login #4.
- Every state-changing request additionally enforces double-submit CSRF
  via the `_csrf` cookie + `x-csrf-token` header (or hidden form field).
- Admin routes require `is_admin = 1`; super-admin routes additionally
  require `role = 2`. Impersonation intentionally blocks GET on
  destructive routes (`/account/delete`, `/account/email`, etc.) — see
  `impersonation.DESTRUCTIVE_*`.

---

## Data model — main entities

```
                    ┌──────────┐
                    │  users   │─────────┐
                    └────┬─────┘         │
          invite_tokens  │   user_sessions (hashed tokens)
                 │       │   subscriptions (per-subproduct)
                 │       │   user_accuracy (leaderboard)
                 │       │   user_predictions (own forecasts)
                 │       │
         ┌───────▼───────▼──────┐
         │    predictions       │ ←── scraped signals + extracted direction
         │ (source_handle,      │
         │  market_id, …)       │
         └───────┬──────────────┘
                 │ resolved=1 triggers:
                 ▼
         ┌──────────────────────┐       ┌──────────────────┐
         │ source_credibility   │       │   market_takes   │ (community)
         │ (time-decayed Brier) │       │  + take_votes    │
         └──────────────────────┘       │  + take_reports  │
                                        │  + resolution    │
                                        │    via predictions
                                        │    outcome       │
                                        └──────────────────┘
```

Other significant tables (not exhaustive):

| Table | Purpose | Added in |
| --- | --- | --- |
| `sessions` | Legacy session table (CSRF token lives here) | 001 |
| `user_sessions` | Hardened session store, hashed tokens | 007 |
| `audit_log` | Append-only admin action log | 006 |
| `password_resets` | Single-use reset tokens, 1h TTL | 001 |
| `notifications` | In-app notification bell | 026 |
| `newsletter_subscribers` | Waitlist with position + referrals | 004 |
| `email_unsubscribes` | One-click unsubscribe index | 002 |
| `credibility_snapshots` | Historical cred values per source | 010 |
| `intelligence_conversations` | Claude chat history | 013 |
| `backtests` | Pro backtesting runs | 015 |
| `user_positions`, `user_bet_history` | Portfolio sync | 017 |
| `collections`, `collection_items` | Spotify-style playlists | 128 |
| `feedback_submissions`, `feature_flags` | Post-MVP product surfaces | 119, 120 |

---

## Admin subsystem

- **Panel** at `/admin` — users, tokens, subscriptions, gifts, impersonation.
- **Moderation** at `/admin/moderation` — take-report queue.
- **Status** at `/admin/status` — incident management.
- **Audit log** at `/admin/audit` — append-only, filterable, CSV export.
- **Feature flags** at `/admin/flags` — per-user or global toggles.
- **Email templates** at `/admin/emails` — live-editable template overrides.
- **Impersonation**: an admin can act as another user; every request
  during impersonation is tagged with both identities in the audit log;
  destructive routes are 403-blocked server-side regardless of admin
  status.

---

## Request lifecycle

```
  HTTP request
      │
      ▼
  [SecurityHeadersMiddleware]    adds CSP, Strict-Transport-Security,
      │                          X-Frame-Options, Referrer-Policy
      ▼
  [SubproductMiddleware]         rejects direct-origin requests that
      │                          bypass Cloudflare (spoofing protection)
      ▼
  [CSRFMiddleware]               double-submit token on POST/PUT/PATCH/DELETE
      │                          (except whitelisted webhook/public POSTs)
      ▼
  [GateMiddleware]               pre-release site-access cookie check;
      │                          public allowlist bypasses
      ▼
  [LoggingContextMiddleware]     injects request_id, client_ip, user_id
      │                          into the structured-log context
      ▼
  FastAPI route handler
      │
      ▼
  db.conn() context (begin / commit / rollback)
      │
      ▼
  response (middleware chain in reverse)
```

Middleware order is preserved across worker restarts; modifying it
requires a server.py edit + a full restart.

---

## Observability

- **Structured logs** via `logging_config.py` — every record carries
  `request_id`, `user_id`, `service`, `environment`. Shipped to Logtail.
- **Sentry** for exceptions (`observability/sentry_setup.py`).
- **`/health`** — liveness check, no DB touches.
- **`/admin/performance`** — slow-query log, request-count rollups.
- **`take_resolution_runs`** table — audit log for the daily take
  resolver; surfaced on the admin panel.
- **Forensic signing** — every list-shaped API response is signed with
  the viewer's per-user seed so leaks are attributable via
  `forensics/extract_watermark.py`.
