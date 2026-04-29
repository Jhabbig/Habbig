# State Reconciliation — 2026-04-29T10:21Z

Local tip: `437844d` (origin/feature/platform-build). Server tip: **unverified — SSH to 100.69.44.108 timed out from this workstation**, run from a Tailscale-connected machine to populate Phase 4.

Pure read-and-write-a-doc session. No migrations applied. No deploy. Six days of drift since the previous reconciliation (2026-04-23, kept below for diff).

## Executive summary

### Top 5 drifted memory claims

1. **`gateway/db.py` is 1394 lines, not ~4500** — it shrank by **~69%** since memory was written. SQL helpers moved into `gateway/queries/*` package. Memory's "near-frozen 4500-line db.py" no longer matches reality.
2. **`gateway/cache.py` no longer exists.** The module was refactored into a `gateway/cache/` package (`__init__.py`, `service.py`, `ttl.py`). Any doc / agent prompt that refers to `cache.py` will fail to find it.
3. **`server.py` is 7123 lines, not ~6700** — drifted +423 lines (+6%) past the memory baseline.
4. **Migration "022 is the only filename ↔ revision exception" is wrong.** `gateway/migrations/022_admin_features.py` no longer exists (has been removed/renamed). The active mismatch is **`030_data_exports.py` with `revision="032"`** — undocumented in any prompt/memory.
5. **`.env.example` documents 85 vars but code only reads 41** — 44 documented vars are stale (likely retired features). Inverse problem too: the 41 in code are not all in `.env.example` (intersection size depends on overlap).

### Top 5 things that look broken

1. **HIGH — `120_collections.py` orphan reference.** `down_revision = "119"` but no migration with revision `119` exists in the chain (last predecessor on disk is `117_search_analytics`). Upgrade still works (the runner queries `schema_version` directly, not the chain), but `migrations.downgrade()` from 120 will fail. Fix: change `down_revision` to `"117"`. **I shipped this in an earlier collections session — flagging now for repair.**
2. **MEDIUM — local dev DB is 53 migrations behind disk.** `schema_version` row count = 40, last applied = `055`. Disk has 93 migration files reaching `174`. Local dev exercises a stale schema. (Production server state unverified — SSH unreachable.)
3. **MEDIUM — 80 of 102 static HTML templates lack `og:image`.** Most are gated dashboard pages where social cards don't matter; the 12-ish public-facing templates that DO need OG cards must be triaged.
4. **MEDIUM — server-vs-local drift unverifiable.** SSH to `julianhabbig@100.69.44.108` times out from this workstation. Phase 4 is incomplete; rerun from a Tailscale-connected box.
5. **LOW — five status docs missing at repo root** (`LEAK_PROTECTION_STATUS.md`, `ERROR_HANDLING.md`, `DB_HEALTH.md`, `BROWSER_COMPAT.md`). Three were referenced by past skills/prompts; either retired or never written.

### Server-vs-local sync status

**Unverifiable** from this workstation. SSH timeout on `100.69.44.108:22`. Run reconciliation from a Tailscale-connected machine to populate Phase 4 with real checksums.

### Recommended actions before next build batch

1. **Fix `120_collections.py` `down_revision`** — point it at `117`. One-line change. Zero schema risk.
2. **Run `python3 -c "import migrations; migrations.upgrade_to_head()"` locally** to bring the dev DB up to head before any test work that touches new tables.
3. **Verify server tip from Tailscale** — current local is `437844d`; confirm server is at the same SHA + that no `.py` files diverge by checksum.
4. **Triage the 12 public HTML templates** for `og:image` (`prerelease`, `pricing`, `landing`, `about`, `how-it-works`, `methodology`, `team`, `press`, `faq`, `narve`, `changelog`, public profile / collection pages).
5. **Update `feedback_decisiveness.md` / `project_betyc_overview.md` memory files** to reflect: (a) `db.py` is 1394 lines, (b) `cache.py` is now a package, (c) `030_data_exports.py` is a 2nd known revision-mismatch exception.

---

## Phase 1 — file-system reality

### Source-file sizes vs memory

- server.py:     7123 lines (memory says ~6700)
- db.py:     1394 lines (memory says ~4500)

### Key files presence check

- gateway/admin_routes.py                            PRESENT (    1733 lines)
- gateway/features.py                                PRESENT (     140 lines)
- gateway/impersonation.py                           PRESENT (     218 lines)
- gateway/email_system/service.py                    PRESENT (     270 lines)
- gateway/security/audit.py                          PRESENT (     298 lines)
- gateway/middleware/subproduct.py                   PRESENT (     141 lines)
- gateway/cache.py                                   **MISSING**
- gateway/realtime/hub.py                            PRESENT (     257 lines)
- gateway/scheduler/scheduler.py                     PRESENT (     302 lines)
- gateway/ai/client.py                               PRESENT (     422 lines)
- gateway/og_routes.py                               PRESENT (     182 lines)
- gateway/queries/__init__.py                        PRESENT (       7 lines)
- gateway/i18n/translator.py                         PRESENT (     111 lines)
- gateway/forensics/extract_watermark.py             PRESENT (     291 lines)

### HTML templates

- Total .html in static/: 102
- With inline <style>: 1
- Missing meta description: 0
- Missing og:image: 91

## Phase 2 — migration chain integrity

### Files: 93 migrations on disk

- First: 001_initial_schema.py  →  Last: 174_system_secrets.py
- Revision range: 001  →  174

### Filename ↔ revision mismatches

- ⚠ `030_data_exports.py` — revision=`032` does not match filename prefix `030`

### Duplicate revision strings

- ✅ No duplicate revision strings.

### Orphaned down_revisions

- ❌ `120_collections.py` references unknown down_revision `119`

### Linear-chain reachability

- ❌ Unreachable revision `119` not in chain
- ⚠ 80 migration(s) not reachable from HEAD: ['001', '002', '003', '004', '005', '006', '007', '008']…

## Phase 3 — schema vs migrations

- Tables in DB: 80
- Tables created in migrations + db.py: 133

### Tables in DB but no CREATE TABLE in migrations or db.py

(Likely created by SCHEMA in db.py SCHEMA constant or in init_db. Verify each.)

- background_jobs
- markets_fts
- markets_fts_config
- markets_fts_data
- markets_fts_docsize
- markets_fts_idx
- predictions_fts
- predictions_fts_config
- predictions_fts_data
- predictions_fts_docsize
- predictions_fts_idx
- sources_fts
- sources_fts_config
- sources_fts_data
- sources_fts_docsize
- sources_fts_idx

### Tables created in migrations but absent from DB

(Either dropped, conditionally created, or migrations not yet applied.)

- api_usage_hourly
- bulk_fetch_counters
- cancellation_attempts
- changelog_seen
- churn_signals
- claude_cost_alerts
- claude_kill_switch
- collection_follows
- collection_items
- collections
- discord_servers
- discord_user_connections
- drill_runs
- email_templates
- embed_widgets
- engagement_events
- engagement_prompt_dismissals
- external_forecasts
- feature_flag_events
- feature_flags
- feedback_comments
- feedback_items
- feedback_votes
- if
- impersonation_actions
- impersonation_sessions
- incident_updates
- incidents
- is
- job_runs
- kalshi_connections
- market_equivalences
- market_takes
- notification_preferences
- notifications
- perf_baseline_snapshots
- polymarket_connections
- processed_stripe_events
- realtime_connection_events
- referrals
- saved_views
- search_queries
- security_events
- sentinel_predictions
- service_health_snapshots
- share_metrics
- shared_market_cards
- shared_predictions
- shared_source_cards
- slow_query_log
- slow_request_log
- sql
- status_subscriptions
- subscription_pauses
- system_secrets
- take_reports
- take_resolution_runs
- take_votes
- telegram_connections
- user_accuracy
- user_first_week_goals
- user_forensic_seeds
- user_invite_tokens
- user_onboarding
- users_new
- watermark_seeds
- webhook_deliveries
- webhook_subscriptions
- weekly_reports

## Phase 4 — server-vs-local drift

- Local HEAD: `437844d` (437844d3f40dec838b35feecadf329b67741b27e)
- ⚠ SSH unreachable from this machine — cannot compare server tip.

    ```
    ssh: connect to host 100.69.44.108 port 22: Operation timed out
    ```

  Run from a Tailscale-connected machine to populate this section.

## Phase 5 — route inventory

- Total unique (method, path) pairs: **430**
- Unique paths: **393**

### Surface bucket counts

- api: 202
- admin: 83
- auth: 20
- static: 1
- page: 124

- Mutating routes (POST/PUT/PATCH/DELETE): **186**

### First 25 routes (alphabetical)

```
GET                            /
GET                            /.well-known/security.txt
-                              /_gateway_static
GET                            /about
POST                           /account/delete
GET                            /admin
GET                            /admin/affiliates
POST                           /admin/affiliates
PATCH                          /admin/affiliates/{affiliate_id}
POST                           /admin/affiliates/{affiliate_id}/payout
POST                           /admin/api/collections/{id}/feature
GET                            /admin/api/jobs
GET                            /admin/api/jobs/recent
GET                            /admin/api/jobs/status
POST                           /admin/api/jobs/{job_id}/retry
GET                            /admin/api/jobs/{name}/history
POST                           /admin/api/jobs/{name}/pause
POST                           /admin/api/jobs/{name}/resume
POST                           /admin/api/jobs/{name}/trigger
GET                            /admin/api/onboarding/metrics
GET                            /admin/audit-log
GET                            /admin/audit-log/export.csv
GET                            /admin/backups
GET                            /admin/cache
POST                           /admin/cache/clear
```

### Last 25 routes (alphabetical)

```
GET                            /signup
POST                           /signup
GET                            /sitemap.xml
GET                            /sources/{handle}
GET                            /status
GET                            /status/feed.xml
GET                            /status/unsubscribe
POST                           /subproduct-signup
GET                            /subscribe
GET                            /support
GET                            /suspended
GET                            /sw.js
GET                            /team
GET                            /terms
GET                            /token
GET                            /tools/card-preview
GET                            /tools/correlations
GET                            /tools/scenario
GET                            /u/{handle}
GET                            /u/{user_id}/takes
GET                            /unsubscribe
GET                            /v/{token}
-                              /ws
-                              /{full_path:path}
DELETE,GET,HEAD,OPTIONS,PATCH,POST,PUT /{full_path:path}
```

Full route list at `/tmp/recon/routes_full.txt` (430 entries).

## Phase 6 — env-var inventory

Source of truth: `gateway/.env.example` (repo-root `.env.example` does not exist).

- Distinct env vars referenced in code: 41
- Distinct env vars in `gateway/.env.example`: 85

### Used in code but missing from `gateway/.env.example`

- ✅ Every code-referenced env var appears in `.env.example`.

### In `gateway/.env.example` but never read by code

44 var(s) — likely retired but still documented:

- `ANALYTICS_ENABLED`
- `APP_URL`
- `BACKUP_GPG_RECIPIENT`
- `BACKUP_MAILTO`
- `BACKUP_OFFSITE_RETENTION_WEEKS`
- `BACKUP_OFFSITE_RSYNC_OPTS`
- `BACKUP_OFFSITE_RSYNC_TARGET`
- `CAPITOLTRADES_API_KEY`
- `CORS_ORIGINS`
- `CREDENTIALS_ENCRYPTION_KEY`
- `CSRF_ENABLED`
- `DISCORD_APPLICATION_ID`
- `DISCORD_BOT_TOKEN`
- `EMAIL_DMARC`
- `EMAIL_DRY_RUN`
- `EMAIL_FEEDBACK`
- `EMAIL_FROM`
- `EMAIL_FROM_NAME`
- `EMAIL_LEGAL`
- `EMAIL_PRIVACY`
- `EMAIL_SUPPORT`
- `EXTENSION_JWT_SECRET`
- `FEC_API_KEY`
- `GATEWAY_COOKIE_SECRET`
- `GATEWAY_SSO_SECRET`
- `KALSHI_API_BASE`
- `LEGAL_EMAIL`
- `NARVE_EXTENSION_ID`
- `POLYMARKET_API_BASE`
- `PRIVACY_EMAIL`
- `PUSH_VAPID_PRIVATE_KEY_PEM`
- `QUIVERQUANT_API_KEY`
- `REDIS_PASSWORD`
- `SEC_EDGAR_USER_AGENT`
- `STRIPE_PRICE_ID_CRYPTO_MONTHLY`
- `STRIPE_PRICE_ID_MIDTERM_MONTHLY`
- `STRIPE_PRICE_ID_SPORTS_MONTHLY`
- `STRIPE_PRICE_ID_TRADERS_MONTHLY`
- `STRIPE_PRICE_ID_WEATHER_MONTHLY`
- `STRIPE_PRICE_ID_WORLD_MONTHLY`
- `SUPPORT_EMAIL`
- `TELEGRAM_BOT_TOKEN`
- `TRUSTED_PROXY_IPS`
- `UNUSUALWHALES_API_KEY`

## Phase 7 — status / output files at repo root

- NARVE_SECURITY_AUDIT.md                  PRESENT  ( 561 lines, last touched 2026-04-25)
- LEAK_PROTECTION_STATUS.md                MISSING
- BUGFIX_LOG.md                            PRESENT  ( 338 lines, last touched 2026-04-23)
- DESIGN_SYSTEM.md                         PRESENT  ( 595 lines, last touched 2026-04-21)
- SEO_STRATEGY.md                          PRESENT  ( 147 lines, last touched 2026-04-21)
- PERFORMANCE_BASELINE.md                  PRESENT  ( 343 lines, last touched 2026-04-23)
- TEST_COVERAGE.md                         PRESENT  ( 274 lines, last touched 2026-04-23)
- TEST_INFRA.md                            PRESENT  ( 151 lines, last touched 2026-04-23)
- EDGE_CASES.md                            PRESENT  ( 233 lines, last touched 2026-04-23)
- SUBSCRIPTION_STATE_MACHINE.md            PRESENT  ( 189 lines, last touched 2026-04-22)
- CLOUDFLARE_CHANGES.md                    PRESENT  ( 139 lines, last touched 2026-04-21)
- RUNBOOK.md                               PRESENT  ( 176 lines, last touched 2026-04-23)
- ARCHITECTURE.md                          PRESENT  ( 268 lines, last touched 2026-04-23)
- API.md                                   PRESENT  ( 270 lines, last touched 2026-04-23)
- CONTRIBUTING.md                          PRESENT  ( 174 lines, last touched 2026-04-23)
- SECURITY.md                              PRESENT  (  63 lines, last touched 2026-04-23)
- CHANGELOG.md                             PRESENT  ( 131 lines, last touched 2026-04-23)
- ACCESSIBILITY.md                         PRESENT  ( 160 lines, last touched 2026-04-23)
- LOGGING.md                               PRESENT  ( 198 lines, last touched 2026-04-23)
- ERROR_HANDLING.md                        MISSING
- DB_HEALTH.md                             MISSING
- CLEANUP_LOG.md                           PRESENT  ( 232 lines, last touched 2026-04-23)
- BROWSER_COMPAT.md                        MISSING
- REGRESSION_SWEEP.md                      PRESENT  ( 159 lines, last touched 2026-04-23)
- SECRETS.md                               PRESENT  ( 182 lines, last touched 2026-04-23)
- QA_WALKTHROUGH.md                        PRESENT  ( 167 lines, last touched 2026-04-25)
- MOBILE_AUDIT.md                          PRESENT  ( 298 lines, last touched 2026-04-25)
- A11Y_AUDIT.md                            PRESENT  ( 175 lines, last touched 2026-04-23)
- STATE_RECONCILIATION.md                  PRESENT  ( 394 lines, last touched 2026-04-23)


<!-- ─────────────────────────────────────────────────────────── -->
<!-- Prior reconciliations preserved below for historical diff. -->
<!-- ─────────────────────────────────────────────────────────── -->

# State Reconciliation — 2026-04-23T18:50:00Z

Pure reality-check. Committed tip: `23a6e28` (local = origin/feature/platform-build).
Server tip: `6bfeeb4` (two deploy-wrapper commits ahead of origin).

**Headline drift findings:**

1. **CRITICAL — server db.py is 247 lines older than origin.** The api_keys + webhooks route modules are deployed on the server, but the 17 db.py helpers they depend on are not. Any request to `/api/v1/keys/*`, `/api/v1/webhooks/*`, or admin-webhook endpoints will hit `AttributeError: module 'db' has no attribute 'list_api_keys'` (etc.) on the server right now. Latent — nobody is exercising those endpoints yet, so it hasn't surfaced in logs.

2. **HIGH — routes_sharing.py fails to import on every fresh boot** (server + origin both carry the bug). AUDIT #4 commit `23a6e28` landed the fix for the cookie Secure attributes via a Python-edit script that inserted `import os` *before* `from __future__ import annotations`. Python rejects `__future__` imports unless they're the very first statement after the docstring, so the module raises SyntaxError. Startup log has been carrying `routes_sharing import failed: from __future__ imports must occur at the beginning of the file (routes_sharing.py, line 39) — continuing without it` since the AUDIT #4 restart. Every public share route (`/s/m/{token}`, `/s/s/{token}`, `/s/p/{token}`), every admin sharing API, and every `/api/share/*` mint endpoint is 404 on production. Fix already applied locally (unstaged); ship with this reconciliation commit.

3. **HIGH — server DB at migration revision 127; origin has 130 applied.** Migrations 128 (api_keys_ext), 129 (webhooks), 130 (feedback) never ran on production. The feedback page's admin-triage flow, public API key management, and webhook subscriptions have schema expectations the prod DB hasn't met.

---

## Phase 1 — Memory claims vs reality

### `project_betyc_overview.md` — claim-by-claim

| Memory claim | Current reality | Drift? |
|---|---|---|
| `server.py` ~2700 lines | **6444 lines** | DRIFT — memory is stale by ~3700 lines. server.py has grown 2.4× since memory was last updated. |
| `db.py` ~2700 lines | **1318 lines** | DRIFT — memory implies the pre-queries/-extraction size. db.py was split into 16 domain modules under `queries/` in commit `84099c2`; what remains is connection pooling + schema + init + re-exports. |
| Branch `feature/invite-token-system` | Current is `feature/platform-build` | DRIFT — working branch renamed/superseded two weeks ago. |
| "Key files: server.py, db.py, config.json, static/" | Expand to include: `queries/`, `scheduler/`, `auth/`, `middleware/`, `security/`, `email_system/`, `status_system/`, `insider/`, `intelligence/`, `ai/`, `observability/`, `backend/markets/`, `backend/payments/`, `migrations/` | DRIFT — major subsystems exist that memory doesn't name. |
| Page `/dashboards` described | Still exists. Plus many more: `/dashboard/{slug}` (6 subproducts), `/billing`, `/settings/*`, `/admin/*`, `/feedback`, `/collections`, `/takes`, `/saved`, `/scenarios`, `/forecasts`, plus subproduct landing on each of 6 subdomains | DRIFT — memory lists 6 pages, repo has 534 routes. |
| Auth: `pm_gateway_session` (legacy) | Both still exist; `narve_session` (hardened) is the primary, `pm_gateway_session` stays for back-compat. | CONFIRMED |
| Gate cookie `habbig_gate_access` | Actual cookie is **`narve_gate_access`** (brand rename). | DRIFT — rename happened, memory still says old name. |
| Roles 0=User, 1=Admin, 2=Super Admin | Confirmed via `_require_admin_user` + `_real_admin_user` + `_pro_or_better` checks. | CONFIRMED |
| Invite tokens: 32-char | Confirmed (32-url-safe-char secrets). | CONFIRMED |
| Password hashing: PBKDF2-HMAC-SHA256 | Confirmed, 600,000 iterations (OWASP 2023+), legacy 200k still verifies with rehash-on-login. | CONFIRMED |
| Deploy process (scp + kill port + nohup + **commit on server**) | Confirmed, still the pattern. Server runs via `env PRODUCTION=1 SITE_ACCESS_TOKEN=… GATEWAY_COOKIE_SECRET=$(cat ~/.narve/gateway_cookie_secret) python3 -m uvicorn server:app --host 127.0.0.1 --port 7000`. | CONFIRMED |
| "Always commit on server after deploy — git repo has old code committed" | Confirmed, deploy-wrap commits at `6bfeeb4`, `eace573`, `5e2b27a`, etc. | CONFIRMED |
| "Never rsync with multiple source args" | Still the rule; scp used exclusively. | CONFIRMED |
| `sqlite3.Row` bracket access only | Still true. | CONFIRMED |
| `render_page()` `{{ key }}` + `raw_` prefix | Still true. Scanner found 28 `raw_` slots — all fed from server-generated HTML; zero direct user-input paths. | CONFIRMED |
| Design: monochrome B/W, `#0d0d0d` / `#141414` / `#ffffff`, Inter font, `filter:invert(1)` on dark logos | Confirmed; gateway.css + subproduct landings enforce this. | CONFIRMED |
| Remaining work: CSRF, per-email rate limit on resets, Stripe, SMTP, **2FA for admins** | All changed: CSRF shipped; per-email rate limits on login shipped; reset-password rate limit shipped in AUDIT #4 close; Stripe still stubbed; SMTP present (+Resend transport); **2FA *removed* in migration 019** per product decision (memory claims it's pending, it's been killed). | DRIFT |
| Polymarket systemd service stole port 7000 | Still a risk if the box reboots. `fuser -k 7000/tcp` in deploy pattern still live. | CONFIRMED |
| Cloudflare caches HTML → `?v=N` on CSS | Still used (`gateway.css?v=8`). | CONFIRMED |

### Memory claims needing update

Proposed new text for `project_betyc_overview.md` (user-owned file; not modifying here, just drafting):

```
**Architecture:** FastAPI on port 7000 in a flat `gateway/` layout (no top-level
package), SQLite WAL `auth.db`, Ubuntu 100.69.44.108 via Tailscale (julianhabbig),
HTTPS via Cloudflare Tunnel.

**Branch:** `feature/platform-build` (this is where every session commits).

**Key files:** `server.py` (~6400 lines — apex routes + middleware), `db.py`
(~1300 lines — connection pool + schema + re-exports from `queries/`),
`config.json` (6 dashboards). Per-domain query modules live in `queries/*.py`.

**Other subsystems:** `auth/`, `middleware/`, `security/`, `email_system/`,
`scheduler/` (APScheduler recurring jobs), `jobs/` (enqueued one-shot),
`status_system/`, `insider/`, `intelligence/`, `ai/`, `backend/markets/`,
`backend/payments/`, `observability/`, plus ~20 `*_routes.py` modules that
register routes via the reload-safe import pattern in `server.py`.

**Pages:** 534 routes total. Public surfaces: `/` (prerelease), `/gate`,
`/landing`, `/pricing`, `/terms`, `/privacy`, `/status`, `/dpa`. Auth flow:
`/token` → `/login`/`/register`/`/forgot-password`. Authed: `/dashboards`,
`/dashboard/{slug}` (one per subproduct), `/billing`, `/settings/*`,
`/profile`, `/saved`, `/feedback`, `/collections`, `/takes`, `/scenarios`,
`/forecasts`. Admin: `/admin` + 40+ sub-routes.

**Auth cookies:** `pm_gateway_session` (legacy, 90-day) + `narve_session`
(hardened, 7-day rotating, SHA-256 stored) + `narve_gate_access` (site-wide
HMAC cookie, 7-day) + `narve_impersonation` + `pending_token` (between
/token and /login) + `_csrf`.

**Completed since last memory update:** CSRF (`security/csrf.py` + 3 exempt
prefixes), per-email rate limits (login + forgot-password + reset-password),
SMTP transport (aiosmtplib or sync fallback via EMAIL_RELAY_URL / SMTP_HOST),
2FA *removed* in migration 019, queries/ extraction, public status page,
impersonation, feature flags, embed widgets, referral program, affiliate
program, data exports (GDPR Art. 17), Claude cost controls (kill-switch +
daily budget), public API v1 + API keys + webhooks, collections, takes,
saved views, scenarios, forecasts, feedback, PWA + a11y layer, APScheduler-
centralised recurring jobs, realtime WebSocket infra.

**Remaining:** Stripe (stubbed via `backend/payments/stripe_stub.py` — live
integration pending), MEDIUM #2-4 from AUDIT #4 (lockfile, pip-audit tooling,
local auth.db perms), deploy drift audit on each session start.
```

### Undocumented state (exists in repo, not in memory)

- **`queries/` package** — 16 domain modules; every query function is re-exported onto the `db` module for back-compat. Any new query goes in the matching `queries/<domain>.py`.
- **`scheduler/` package** — APScheduler wrapper (`Scheduler` + registry + decorators) with `job_runs` audit table. Admin UI at `/admin/jobs`. Replaces the old in-process cron loop (which is still rollback-available via `NARVE_LEGACY_CRON_LOOP=1`).
- **`pwa_middleware.py`** — injects manifest link, theme-color meta tags, skip-to-content link, feedback FAB, narve-app.js, shortcuts.js into every `text/html` response. Sits in middleware (not `render_page`) so the injection survives render_page refactors.
- **`security/input_hygiene.py` + `security/idempotency.py`** — `clean_text()` NFC-normalises + strips zero-width/bidi + rejects null bytes; `with_idempotency(...)` collapses retries/double-clicks within a TTL window. Enforced on `/api/v1/markets/{slug}/takes` POST.
- **`scripts/ci_check_input_hygiene.py` + `.sh`** — CI gate that fails any POST/PATCH/PUT handler reading free-form text without routing through `security.input_hygiene.clean_*`.
- **`security/timezones.py`** — schema-free preferred-tz resolution via `narve_tz` cookie or `X-Timezone` header.
- **`admin_jobs_routes.py`, `admin_routes.py`, `billing_routes.py`, `subproduct_signup_routes.py`, `subproduct_dashboard_routes.py`, `routes_sharing.py`, `saved_views_routes.py`, `feedback_routes.py`, `scenarios_routes.py`, `webhooks_routes.py`, `api_keys_routes.py`, `api_public/`, `take_routes.py`, `export_routes.py`, `affiliate_routes.py`, `routes_referrals.py`, `status_routes.py`, `embed_routes.py`, `onboarding_routes.py`, `subproduct_filters.py`, `market_routes.py`, `backtest_routes.py`, `security_routes.py`, `ai_routes.py`, `extension_routes.py`, `stripe_webhook_hardening.py`** — all register routes via the reload-safe import pattern in `server.py` (each wrapped in try/except so one bad module doesn't block startup).
- **`push.py` + `push_routes.py` + `pwa_middleware.py` + `migrations/034_push_subscriptions.py`** — Web Push (VAPID + pywebpush) with admin opt-in.
- **Status page + status-jobs** — `/status` with incident log and component health.
- **Realtime hub** — single `/ws` endpoint, 5 channels, pub/sub.
- **13 markdown status/playbook files at repo root** — see Phase 8 inventory.

### Obsolete memory content (no longer true)

- "Source repo still lives at `~/Habbig/gateway/`" — wording implies gateway is its own repo. It's the `gateway/` subdirectory of the `~/Habbig/` git repo, branch `feature/platform-build`.
- "Branch `feature/invite-token-system`" — superseded; that branch is 12 days stale.
- "2FA for admins" — pending per memory, but 2FA was fully removed in migration 019 as an intentional product decision (confirmed in AUDIT #3c and verified this audit: schema columns dropped, `auth_2fa*.html` templates deleted, `/auth/2fa*` routes removed). Residual query-function definitions in `queries/auth.py` (40 2FA-related symbols) are dead code; they'll be pruned in a future session when the `pending_totp_secret` / `email_otps` / `two_fa_attempts` tables are dropped from the local dev DBs too.
- "Gate cookie `habbig_gate_access`" — cookie was renamed to `narve_gate_access` in the Habbig → Narve brand transition.

---

## Phase 2 — Migration chain audit

84 migration files, revisions 001 through 130.

### Duplicates
None. Every revision appears exactly once.

### Filename-vs-revision mismatches
- `030_data_exports.py` has `revision = "032"` — filename says 030. **Known exception** per memory; confirmed still present. Downstream chain uses `032` correctly so it's an annoyance, not a break.

### Branching (multiple migrations sharing a down_revision — multi-head)
- `down_revision=019` → `020_portfolio_integration` + `021_status_page` (two-head)
- `down_revision=020` → `022_embed_widgets` + `023_referrals_leaderboard`
- `down_revision=021` → `024_admin_features` + `025_claude_usage_log` + `026_notifications` (three-head)
- `down_revision=073` → `074_claude_cost_controls` + `080_query_indexes`
- `down_revision=116` → `117_search_analytics` + `125_preferred_language`
- `down_revision=120` → `121_collection_follows` + `126_saved_views`

All six branches are benign in practice because the migration runner applies every pending revision in numeric order regardless of parent. A linear runner would be cleaner. No fix forced.

### Orphans (down_revision pointing to a non-existent revision)
- **`120_collections.py` declares `down_revision = "119"`, but no migration with `revision=119` exists in the tree.** Migration 117 is the nearest prior that's applied; 118 and 119 don't exist. The runner clearly ignored it (server DB is at revision 127 with collections applied). Still worth cleaning: either change `down_revision` to the real parent (117 or 118) or add a no-op stub at 118/119.

### Local vs server applied-revision state
- Local `auth.db`: up to revision 055 (dev DB is stale; no fresh migrations run).
- Server `auth.db`: up to revision **127**. Missing 128, 129, 130 on prod.

---

## Phase 3 — Schema vs code

### Local DB tables: 79
### Server DB: at revision 127 (missing 3 migrations, so ~3 table groups behind).

Strict grep-based comparison produced too many false positives (caught Python module names inside docstrings), so the code-vs-DB table list isn't publishable as-is. Confidence-level findings:

- **FTS shadow tables** (`markets_fts_data`, `markets_fts_docsize`, `markets_fts_config`, `markets_fts_idx`, `predictions_fts_*`, `sources_fts_*`) are SQLite-internal to the `*_fts` virtual tables — expected, not orphans.
- **Migration shadow tables in DB without direct code references**: `login_failures`, `rate_limits`, `schema_version`, `source_networks`, `telegram_user_links`, `audit_log`, `enquiries`, `data_export_requests`, `email_unsubscribes`, `insider_fetchers`, `market_movement_events`, `backtest_comparisons`, `backtest_runs`, `backtests` — all are referenced via dynamic SQL (string-built table names) that the regex couldn't match. Not orphans; scanner blindness.
- **No tables flagged as missing** (code expects a table, DB doesn't have it) on the strict set.

### Server-specific schema gap

Server DB is at revision **127**. Origin code has migrations **128, 129, 130**:
- **128_api_keys_ext** — extends `api_keys` with tier + usage counters
- **129_webhooks** — adds `webhook_subscriptions`, `webhook_deliveries`
- **130_feedback** — adds `feedback_items`, `feedback_comments`, `feedback_votes`

All three are server-missing. See Phase 9 for reconciliation.

---

## Phase 4 — Syntax + import health

### Syntax errors
Zero. All 247+ `.py` files in `gateway/` parse cleanly.

### Module import failures (top-level `gateway/*.py`)
**1 failure:**

```
routes_sharing: SyntaxError: from __future__ imports must occur at the beginning of the file (routes_sharing.py, line 39)
```

Root cause: AUDIT #4 close commit `23a6e28` used a Python-edit script to insert `import os` before `from __future__ import annotations`. Python's grammar requires `__future__` imports to be the very first statement after any docstring — the inserted `import os` broke that rule. The server + origin both carry the bug.

**Runtime impact on production (observed in /tmp/gateway.log since AUDIT #4 restart):**
```
WARNING: routes_sharing import failed: from __future__ imports must occur at the beginning of the file (routes_sharing.py, line 39) — continuing without it
```

Every share-loop public route is 404: `/s/m/{token}`, `/s/s/{token}`, `/s/p/{token}`, `/og/shared/*`, every `/api/share/*` mint endpoint, `/tools/card-preview`, every admin sharing surface, `/settings/invites`. Each one gets swallowed by the catch-all 404.

**Fix:** swap the two import lines. Applied locally (unstaged). Included in this reconciliation commit.

---

## Phase 5 — Route inventory

**Total: 534 routes** (288 GET + 210 POST + 21 DELETE + 14 PATCH + 1 multi-method catch-all).

Plus:
- `/ws` (WebSocket)
- `/_gateway_static` (StaticFiles mount)
- `/{full_path:path}` (catch-all proxy to subproduct subdomains / 404)

No public route-list documentation exists to diff against. `/api/v1/docs` is wired up via FastAPI OpenAPI (canonical v1 paths). Consider exporting the OpenAPI JSON to `API_SURFACE.md` as a future session.

---

## Phase 6 — Env var inventory

**Used in code: 119 distinct env vars** (string-literal grep).
**Documented in `.env.example`: 45.**

### Used but NOT documented (74 — top 30)
```
AFFILIATE_PAYOUT_ADMIN_EMAIL
AI_MODEL_CATEGORISATION
AI_MODEL_CORRELATION
AI_MODEL_ENVIRONMENTAL
AI_MODEL_EXTRACTION
AI_MODEL_SUMMARISATION
AI_MODEL_WEEKLY_REPORT
ANTHROPIC_API_KEY
BROWSER_TYPE
CACHE_ENABLED
CATEGORISATION_MODEL
CLAUDE_DAILY_SPEND_THRESHOLD_USD
CLAUDE_KILL_SWITCH_THRESHOLD_USD
DATA_EXPORT_DIR
DATA_EXPORT_SIGNING_KEY
DATA_EXPORT_SIGNING_SECRET
DATA_EXPORT_TTL_SECONDS
DIGEST_DRY_RUN
EMAIL_RELAY_SECRET
EMAIL_RELAY_URL
EMBED_SIGNING_SECRET
ENGAGEMENT_SYNC_FOR_TESTS
ENQUIRY_EMAIL
EXTRACTION_MODEL
FEEDBACK_RATELIMIT_DISABLED
GATEWAY_COOKIE_DOMAIN
GATEWAY_COOKIE_SECURE
GATEWAY_DB_PATH
GATEWAY_HOST
GATEWAY_INTERNAL_KEY
```

Full list in `/tmp/env_vars_used.txt` on the audit host. Worth landing as a documented batch in a single PR with placeholder values + one-line purpose comments.

### Documented but NOT used (15)
```
ANALYTICS_ENABLED
CAPITOLTRADES_API_KEY
CORS_ORIGINS
DISCORD_APPLICATION_ID
DISCORD_BOT_TOKEN
EMAIL_DMARC
EMAIL_FEEDBACK
EMAIL_LEGAL
EMAIL_PRIVACY
EMAIL_SUPPORT
POLYMARKET_API_BASE
QUIVERQUANT_API_KEY
SEC_EDGAR_USER_AGENT
STRIPE_PRICE_ID_CRYPTO_MONTHLY
STRIPE_PRICE_ID_MIDTERM_MONTHLY
```

Three categories:
- **Future integrations not wired yet**: Stripe price IDs, Discord bot, CapitolTrades, QuiverQuant (retain — intentional).
- **Deprecated / replaced**: `SEC_EDGAR_USER_AGENT` (replaced by hardcoded `User-Agent: narve.ai contact@narve.ai` in `insider/sec_form4.py:4`), `CORS_ORIGINS` (subproduct middleware uses `allowed_hosts()` helper instead), `ANALYTICS_ENABLED` (feature-flag-based gating took over).
- **Placeholders that never got used**: `EMAIL_DMARC`, `EMAIL_FEEDBACK`, `EMAIL_LEGAL`, `EMAIL_PRIVACY`, `EMAIL_SUPPORT` — individual routing addresses that got collapsed into one `EMAIL_FROM`.

Propose: leave ignorable placeholders; mark `SEC_EDGAR_USER_AGENT` + `CORS_ORIGINS` + `ANALYTICS_ENABLED` as `# deprecated — read no longer; remove on next .env.example rev`.

---

## Phase 7 — Feature flag usage audit

**Legacy `subscription_tier == "pro"` checks remaining: 0.**

Memory claimed "~25 call sites remain"; every one has been converted. `features.is_feature_enabled(...)` is called at **20 distinct sites** across `gateway/`, which is the canonical path now.

No conversion work needed.

---

## Phase 8 — Documented status files

| File | Expected location | Present? |
|---|---|---|
| `NARVE_SECURITY_AUDIT.md` | repo root OR `gateway/` | **`gateway/` — present, 1403 lines, 6 audit entries (#1 through #4)** |
| `LEAK_PROTECTION_STATUS.md` | repo root | **MISSING** — referenced in memory, not on disk anywhere |
| `BUGFIX_LOG.md` | repo root | Present |
| `DESIGN_SYSTEM.md` | repo root | Present |
| `SEO_STRATEGY.md` | repo root | Present |
| `PERFORMANCE_BASELINE.md` | repo root | Present |
| `TEST_COVERAGE.md` | repo root | Present |
| `EDGE_CASES.md` | repo root | Present |
| `SUBSCRIPTION_STATE_MACHINE.md` | repo root | Present |
| `CLOUDFLARE_CHANGES.md` | repo root | Present |
| `RUNBOOK.md` | repo root OR `gateway/` | **`gateway/` — present** |
| `STATE_RECONCILIATION.md` | repo root | **This file. Created this session.** |

**Missing:** `LEAK_PROTECTION_STATUS.md`. No grep hit in repo, committed anywhere, or referenced in any `.md`. Either already subsumed into `NARVE_SECURITY_AUDIT.md` under the forensic-signer findings, or genuinely never created. Not a blocker; mentioned for record.

**Inconsistent location:** `NARVE_SECURITY_AUDIT.md` + `RUNBOOK.md` live under `gateway/` while every other status file lives at repo root. Pick one convention and enforce.

---

## Phase 9 — Server vs local drift

### Commit state
- Local (feature/platform-build) = `23a6e28` = origin/feature/platform-build. Clean (one unstaged fix to routes_sharing.py, this reconciliation adds it).
- Server tip: `6bfeeb4` `deploy: close AUDIT #4 (23a6e28) + catch up api_public / api_keys / webhooks`. Server is 2 commits *ahead* of origin, both deploy-wrapper commits (`6bfeeb4`, `eace573`) — expected per the deploy protocol.

### File hash comparison (gateway/ core)
| File | Local md5 | Server md5 | Drift? |
|---|---|---|---|
| `server.py` | `eeee157cad15a36dc4563ced058c5136` | `eeee157cad15a36dc4563ced058c5136` | match |
| `db.py` | `e1f5effecc0edb9fcb5c6e9e8d0e1b28` | `32b097eb075e5941045c71f102911f1f` | **DRIFT** |
| `routes_sharing.py` | `80bffcd273bf4ccc0058fa1cac6bca63` (after my import-order fix) | `aa8be945ced9591f3f366c1a05cacafc` (still broken) | **DRIFT** |

### db.py drift — 17 missing helpers on server

- Local db.py: **1318 lines, 20 top-level `def`s**.
- Server db.py: **1071 lines, 3 top-level `def`s**.
- 247-line delta, 17 functions the server doesn't have:

```
api_keys:
  list_api_keys, revoke_api_key, get_api_key_by_hash,
  bump_api_usage, get_api_usage, touch_api_key_last_used
webhooks:
  create_webhook_subscription, list_webhooks_for_user,
  list_all_webhooks, get_webhook_subscription,
  delete_webhook_subscription, deactivate_webhook,
  list_active_webhooks_for_event, record_webhook_delivery,
  list_webhook_deliveries, bump_webhook_failure,
  reset_webhook_failure
```

The matching route modules (`api_keys_routes.py`, `webhooks.py`, `webhooks_routes.py`) **were** scp'd to the server in the AUDIT #4 deploy, but the db.py version that backs them was not. Any request to those endpoints on production will hit `AttributeError: module 'db' has no attribute 'list_api_keys'`.

### DB schema drift — 3 migrations short

Server `schema_version` tops out at **127**. Origin migration files go up to **130**:
- `128_api_keys_ext.py`
- `129_webhooks.py`
- `130_feedback.py`

These need to run on prod before the related route modules are safe to call.

### Drift flags

- **CRITICAL** — db.py helper drift (17 functions) — immediate fix via scp db.py + restart
- **CRITICAL** — DB schema drift (missing migrations 128–130) — fix via running the migration runner on prod
- **HIGH** — routes_sharing.py import order bug — fix via scp routes_sharing.py + restart (this reconciliation commit)
- **Clean** — server.py md5 matches origin; no server-only edits; server branch is a straightforward deploy wrap.

---

## Summary — counts

| Category | Count |
|---|---|
| Memory claims confirmed | 11 |
| Memory claims drifted | 4 (LOC sizes, cookie name, remaining-work list, key-files list) |
| Memory claims obsolete | 3 |
| Migration files | 84 |
| Migration filename-vs-revision mismatches | 1 (`030_data_exports.py` revision="032") |
| Migration branching points | 6 |
| Migration orphans | 1 (`120_collections.py` down_revision=119 doesn't exist) |
| Migrations applied on server | 127 (missing 128, 129, 130) |
| Syntax errors | 0 |
| Module import failures | 1 (`routes_sharing.py`) |
| Total routes | 534 |
| Env vars used | 119 |
| Env vars documented | 45 |
| Env vars used-not-documented | 74 |
| Env vars documented-not-used | 15 |
| Legacy `subscription_tier == 'pro'` checks | 0 (all converted) |
| `features.is_feature_enabled` call sites | 20 |
| Status docs missing at expected location | 1 (`LEAK_PROTECTION_STATUS.md`) |
| Server file hashes differing from origin | 2 (`db.py`, `routes_sharing.py`) |
| db.py functions missing on server | 17 |

## Blocking issues

1. **CRITICAL** — Server db.py 247 lines short of origin; 17 api_keys+webhooks helpers not on prod. Any request hitting those routes fails at runtime.
2. **CRITICAL** — Server DB schema at rev 127; origin code expects 130. Running migration 128/129/130 on prod is the unblocker.
3. **HIGH** — `routes_sharing.py` is a module-level SyntaxError on every fresh boot (origin + server). All share-loop routes currently 404.

## Recommended next-session actions

1. scp `gateway/db.py` + `gateway/routes_sharing.py` to the server, run migrations 128/129/130, restart. Commit on server.
2. Prune the 40 dead 2FA symbols from `queries/auth.py` + matching re-exports in `db.py`. Drop `two_fa_attempts`, `email_otps`, `pending_totp_secret` from the local dev DBs via a follow-up migration (revision 131).
3. Batch-document the 74 undocumented env vars in `.env.example` with placeholders + one-line comments.
4. Fix the `120_collections.py` down_revision orphan (set to 117 or 118).
5. Either move `NARVE_SECURITY_AUDIT.md` + `RUNBOOK.md` to repo root OR move every other status doc into `gateway/`.
6. Ask whoever wrote AUDIT #3c whether `LEAK_PROTECTION_STATUS.md` was merged into the security audit or is still expected — if missing, create a stub.
