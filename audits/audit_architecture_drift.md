# Audit: ARCHITECTURE.md drift vs. actual code

**Scope:** Compare claims in `/Users/shocakarel/Habbig/ARCHITECTURE.md` against
the current state of the repo on `feature/platform-build`. Spot-check port
numbers, table names, route paths, file paths, line counts, file counts, and
constants.

**Method:** No code changes. Read the doc, then verify each load-bearing claim
against the source tree via `ls`, `grep`, and `wc -l`. Findings below are
ordered by severity (`HIGH` = factually wrong and likely to mislead an
operator; `MEDIUM` = stale count/path; `LOW` = cosmetic).

---

## Summary

- **Drift count:** 16 distinct claims diverge from reality.
- **Severity breakdown:** 5 HIGH, 8 MEDIUM, 3 LOW.
- **Net assessment:** The high-level shape (gateway → SQLite → 13 subproducts
  on private localhost ports) is still correct. The drift is concentrated in
  (a) file/module counts that have grown since the doc was last refreshed,
  (b) the `dashboard_key ≠ slug` claim being undercounted, and (c) the
  session-TTL constant being off by ~13× (90d vs. doc's 7d).

---

## Top 3 outdated claims

1. **HIGH — Session TTL is documented as 7 days, actually 90 days.**
   `ARCHITECTURE.md:164` reads `long-lived narve_session (7d, HttpOnly, …)`.
   `gateway/queries/auth.py:29` defines `SESSION_TTL = 90 * 24 * 60 * 60  # 90 days (3 months)`.
   `SESSION_HARDENED_TTL` is 7 days (`queries/auth.py:38`), so the doc likely
   confused the two constants. Material for any incident reviewer trying to
   reason about session lifetime.

2. **HIGH — `dashboard_key ≠ slug` for 3 subproducts, not 1.**
   `ARCHITECTURE.md:82` says *"`traders` is the only subdomain where
   `dashboard_key` ≠ `slug`"*. Actual reality from
   `gateway/subproduct.py`:
   - `traders` → `dashboard_key = "top_traders"`
   - `cb` → `dashboard_key = "centralbank"`
   - `health` → `dashboard_key = "world_health"`
   Anyone reading the doc to wire up a new dashboard entitlement check will
   under-bridge two of the three exceptions.

3. **HIGH — `love-dashboard/` is no longer scaffold-only.**
   `ARCHITECTURE.md:85` says *"`love-dashboard/` currently contains only
   `Dockerfile`, `requirements.txt`, and `data/`. The catalogue entry, port
   reservation, and Cloudflare routing land in the same release window as
   the first server.py commit."*
   Actual contents include `server.py` (545 lines, 10+ routes, `@app.get("/")`,
   `/api/metrics`, `/api/trends`, `/api/compare`, `/api/health`, etc.),
   `schema.sql`, `love.sqlite`, `observability.py`, `static/`. The status
   column in the catalogue still reads "MVP (new — scaffold only)" — the
   subproduct has moved past scaffold.

---

## Full drift list

### HIGH

1. **Session TTL** (`L164`) — claim `7d`, actual `90d` (`queries/auth.py:29`).
   `SESSION_HARDENED_TTL` is the 7d constant; the doc has them swapped.

2. **`dashboard_key ≠ slug` exceptions** (`L82`) — claim 1, actual 3
   (`traders`, `cb`, `health`).

3. **`love-dashboard/` scaffold claim** (`L85-87`) — file inventory wrong;
   `server.py` is a full app, not absent.

4. **`server.py` line count** (`L102`) — claim `7324`, actual `8639`
   (`wc -l gateway/server.py`). +18% drift.

5. **`db.py` line count** (`L102`) — claim `1394`, actual `1533`
   (`wc -l gateway/db.py`). +10% drift.

### MEDIUM

6. **`gateway/migrations/` count** (`L112`) — claim `94`, actual `108`
   (`ls gateway/migrations/ | wc -l`). 14 new migrations since the doc was
   last refreshed (latest verified: `124_take_resolution.py`, `130_*` likely
   present given the gap).

7. **`gateway/queries/` count** (`L104`) — claim `21 modules`, actual `28`
   (`ls gateway/queries/`). The doc's enumerated list also omits the now-present
   `ai_cost.py`, `analytics.py`, `integrations.py`, `jobs.py`,
   `search_analytics.py`.

8. **`gateway/jobs/` count** (`L110`) — claim `30 modules`, actual `33`
   (`ls gateway/jobs/`). New since doc: at least
   `compute_churn_signals.py` is in the doc but the count was 30 — current is
   33 including `affiliate_jobs.py`, `feedback_digest.py`,
   `share_retention.py`, others.

9. **`gateway/security/` count** (`L107`) — claim `7`, actual `8`
   (`ls gateway/security/`). The new file is `logger.py` (not enumerated in
   the doc's role string).

10. **`gateway/email_system/` count** (`L109`) — claim `5`, actual `7`
    (`ls gateway/email_system/`). New: `watermark.py`, `welcome.py`.

11. **`gateway/i18n/locales/` count** (`L111`) — claim `4 locales × 262 keys`,
    actual `4 locales × 262 keys` BUT only when the wildcard `candidates.json`
    is excluded; the directory listing also contains a non-locale file. Count
    is fine but the docstring would mislead a reader who runs `ls` and sees 5
    entries. (Minor; downgrading to MEDIUM only because of confusion risk.)

12. **`gateway/db_collections.py` reference** (`L243`) — claim file exists as
    a per-feature DB layer. Actual: no such file. `gateway/db_*.py` are
    only `db_affiliate.py`, `db_forecasts.py`, `db_referrals.py`,
    `db_sharing.py`, `db_takes.py`. Collections moved into
    `queries/collections.py` during the decomposition.

13. **Auth subsystem file naming** (`L107`) — doc claims the directory holds
    *"Cookies, guards, middleware, session hardening"*. Actual:
    `cookies.py`, `guards.py`, `middleware.py`, `__init__.py`. There is no
    distinct session-hardening file — that lives in `queries/auth.py` (e.g.
    `SESSION_HARDENED_TTL`). The four-file claim count is right but the
    "session hardening" file is misleading.

### LOW

14. **`subproduct.py` docstring is outdated, not ARCHITECTURE.md itself, but
    the doc reflects the same drift.** The docstring at
    `gateway/subproduct.py:3` says *"Each of the six subdomains (sports,
    weather, world, crypto, midterm, traders)…"* — catalogue has 13. Not a
    doc bug strictly, but ARCHITECTURE.md's catalogue rows past row 6 were
    added without updating the prose elsewhere.

15. **`/admin/performance` endpoint claim** (`L267`) — claim it is a route.
    Actual: no `@app.get("/admin/performance")` decorator exists. The string
    is referenced from `cache/service.py:18`, `queries/query_tracer.py:31`,
    and `migrations/081_slow_query_log.py` as the *consumer* of the slow-query
    log, but the route itself is registered elsewhere (admin shell) or via
    template lookup; ARCHITECTURE should specify where.

16. **Tailscale IP `100.69.44.108`** (`L25`) — not referenced anywhere in
    `gateway/`. Found only in three peer markdown files
    (`CLOUDFLARE_CHANGES.md`, `REGRESSION_SWEEP.md`,
    `STATE_RECONCILIATION.md`). If the IP changes, this doc is one of four
    places to update; consider a single source of truth.

---

## Claims spot-checked and FOUND ACCURATE

- Gateway port `:7000` — consistent with `start_dashboards.sh` and config.
- Staging port `:7001` — `server.py:996` `STAGING_BACKEND_URL` default
  matches.
- Subproduct ports `8888, 5050, 7050, 8000, 8051, 8052, 7051, 7052, 7060,
  8053, 7061, 7053, 7062` — verified against
  `admin_health_monitor_routes.py:56-60` and individual
  `subproduct/server.py` `uvicorn.run(port=…)` lines.
- `MAX_SESSIONS_PER_USER = 3` — confirmed at `queries/auth.py:41`.
- `019_remove_2fa.py` — file exists at `gateway/migrations/019_remove_2fa.py`.
- `take_resolution_runs` table — created in
  `gateway/migrations/124_take_resolution.py:33`.
- Middleware names — `SecurityHeadersMiddleware`, `CSRFMiddleware`,
  `SubproductMiddleware`, `GateMiddleware`, `LoggingContextMiddleware` all
  registered in `server.py` in the documented order.
- Per-feature DB modules `db_takes.py`, `db_affiliate.py`, `db_forecasts.py`,
  `db_referrals.py`, `db_sharing.py` — all exist.
- Route module files — every route module enumerated at `L118-121`
  (`market_routes`, `take_routes`, `user_prediction_routes`, `billing_routes`,
  `admin_routes`, `intelligence_routes`, `subproduct_dashboard_routes`,
  `subproduct_signup_routes`, `forecast_routes`, `portfolio_routes`,
  `affiliate_routes`, `collections_routes`, `notification_routes`,
  `scenarios_routes`, `embed_routes`, `feedback_routes`, `webhooks_routes`,
  `api_v1`, `api_keys_routes`) is present under `gateway/`.
- `observability/sentry_setup.py` — exists, gated on `SENTRY_DSN`.
- Stripe test mode — `config.py` validator hints at `sk_test_*` defaults;
  no `sk_live_` references in gateway.

---

## Recommended fixes (for a follow-up doc-only change)

1. Update `L164` to say `90d` (or rephrase to note both `SESSION_TTL=90d` and
   `SESSION_HARDENED_TTL=7d` for the secondary cookie).
2. Update `L82` note to enumerate all three exceptions (`traders → top_traders`,
   `cb → centralbank`, `health → world_health`).
3. Update `L79` and `L85-87` for `love-dashboard` — drop the
   "scaffold only" caveat and flip status to MVP (or whatever stage it is).
4. Refresh line counts at `L102` and module counts at `L104-112` (or convert
   to a "regenerate via `scripts/audit/architecture_counts.py`" pattern so
   they don't rot).
5. Remove the `db_collections.py` row at `L243` or replace it with a pointer
   to `queries/collections.py`.
6. Update the `subproduct.py:3` docstring in lockstep with any
   ARCHITECTURE.md refresh — keep the prose consistent across both.

No code changes were made by this audit.
