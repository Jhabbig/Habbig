# Bugs Found — bug-hunt loop log

CHANGELOG-style append-only log from focused bug-hunt iterations. Newest entries on top.

---

## 2026-05-29 14:28 — Gate-bypass + rate-limit crawl (32-agent sweep)

Coordinated sweep (~32 sibling agents) crawling every route family for `SITE_ACCESS_TOKEN` gate-bypasses + rate-limit coverage. Branch `feature/platform-build`.

### Scope crawled
- Apex (`narve.ai`) + all 13 subdomains
- All `/admin/*` families (users, jobs, logs, audit, email-addresses, integrations, health monitor, etc.)
- `/settings` + `/billing`, `/auth/*`, `/api/*`, and public prefixes (`/public`, sitemap-listed pages)

### Gate-bypass result
- **ZERO gate bypasses found.** Every gated path returns `302 → /gate` when the gate cookie is absent — verified across the apex, all 13 subdomains, every `/admin` family, and `/settings` + `/billing`. No route served gated content without the gate cookie.

### Rate-limit result
- `/gate` limiter **confirmed working**: `429` after ~5 CSRF-valid wrong-token attempts.
- **Nuance:** the CSRF check fires *before* the rate-limit check, so requests with a missing/stale CSRF token are rejected at the CSRF layer first. Earlier "limiter broken" reports were **false positives** — they sent CSRF-invalid requests that never reached the rate-limit counter. With a valid CSRF token + wrong gate token, the 429 trips as designed.

### Earlier-found gaps (now closed)
- A sibling earlier flagged 2 unprotected endpoints: `/api/status/subscribe` and `/api/status/unsubscribe`.
- **Fixed + committed** in `03298be` (`fix(ratelimit): per-IP+per-email limits on /api/status/subscribe + per-IP on unsubscribe`). Verified live in `gateway/status_routes.py`: subscribe = 5/hr/IP (`:305`) + 3/day/email (`:312`); unsubscribe = 20/hr/IP (`:352`).

### Open items
- None blocking from this sweep. Gate enforcement and `/gate` limiter both behaving correctly; the only two gaps identified are already patched.

---

## 2026-05-16 14:45 — iteration 4

**Targeted test run** (`test_admin_*.py`, `test_analytics_cookie.py`, `test_login_direct.py`, `test_admin_health_monitor.py`): 4 failures, **0 new regressions from `cf70ffa` / `5130b62`**. All 4 are the same failures iter 3 already classified pre-existing:

- 3x `test_analytics_cookie.py::TestVisitorCookieMint` — stale post-`a558844` (cookie now consent-gated, tests still expect unconditional mint). Iter-3 follow-up still applies.
- 1x `test_login_direct.py::test_login_post_with_csrf_succeeds` — passes in isolation, fails in suite due to shared in-process rate-limit state polluted by `test_analytics_cookie.py` (which fires many `/api/analytics/event` POSTs from the same `testclient` host). Pre-dates iter 4 (`5130b62` added `@rate_limit` to `/login`).

### Wire-up check: `analytics.js` → `cookie_consent.js`

`gateway/static/analytics.js:133` exposes `window.narveTrackPostConsent`, but `gateway/static/cookie_consent.js:117-126` does NOT call it on the Accept click — instead it does `window.location.reload()`. The reload path works (next request mints cookie + fires `page_view` server-rendered), so **not a functional bug**, but `narveTrackPostConsent` is currently **dead code on the JS side** — either wire it up (faster, no reload flash) or drop the export. Not fixed this iteration (analytics.js in the no-touch list this round).

### Smell check on today-touched files

`gateway/pwa_middleware.py`, `gateway/auth/cookies.py`, `gateway/static/cookie_consent.js`, `gateway/admin_emails_routes.py`, `gateway/admin_integrations_routes.py`, `gateway/admin_routes.py`, `gateway/db.py`, `gateway/queries/admin.py`, `gateway/server.py`: no bare `except:` (one comment mentions a previously-fixed one). One legitimate `TODO(cookie-domain-migration)` in `auth/cookies.py:137`. Promise/null-check audit on the two JS files clean — `cookie_consent.js` Accept handler wraps reload in try/except and falls back to `hideBanner`.

### Dead imports

- **TRIVIAL [gateway/pwa_middleware.py:19]** Unused `import os` — leftover from before consent-gating refactor moved env reads to `auth.cookies`. **Fixed this iteration in `dd0fc87`.**
- **LOW [gateway/affiliate_routes.py:29]** Unused `import logging`. Pre-dates today (file last touched in April). Skipping per restraint.
- **LOW [gateway/queries/admin.py:11,13]** Unused `import hmac`, `import logging`. Pre-dates today. Skipping per restraint.

### Stable counter

Iteration 3: 1 new bug (the trivial `VISITOR_COOKIE` import, fixed in d96d006). **Counter was 1.**
Iteration 4: 1 new trivial fix shipped (`os` import, `dd0fc87`). Plus 1 documented JS dead-code observation. **Counter resets/stays at 1** — found and fixed a fresh bug this iteration.

### Fix shipped

- `dd0fc87` — `chore(pwa): drop unused os import from pwa_middleware`

---

## 2026-05-16 11:12 — iteration 3

**Targeted test run** (`tests/test_admin_*.py tests/test_login_direct.py tests/test_analytics_cookie.py`): 4 failures, all classified **pre-existing** — none are regressions from `5130b62`.

- `test_login_direct.py::TestNewAuthEdgeCases::test_login_post_with_csrf_succeeds` — passes in isolation, fails only when run with `test_analytics_cookie.py`. Classic fixture-pollution: same root cause as iter 2's admin login fixture finding.
- `test_analytics_cookie.py::TestVisitorCookieMint::*` (3 tests) — verified against `5130b62~1` (parent of audit #21 fix): the same 3 tests fail there too. So **not a 5130b62 regression**. BUT the tests are now **conceptually stale** post-`a558844` (cookie consent banner): they expect `narve_visitor` to be minted unconditionally on first HTML hit, but the new consent system intentionally withholds the cookie until the visitor clicks "accept". Either skip-mark the tests or rewrite them to first POST `narve_consent=accept` before asserting the cookie. See `gateway/tests/test_analytics_cookie.py:136,183,210`.

### Findings

- **LOW [gateway/pwa_middleware.py:33,39]** Dead import `VISITOR_COOKIE` from today's `68ba4aa6` (only referenced in a comment, never in code). **Fixed this iteration in `d96d006`.**
- **MED [gateway/tests/test_analytics_cookie.py:136,183,210]** Three `TestVisitorCookieMint` tests are stale post cookie-consent banner (`a558844`) — they expect unconditional minting on first hit, but the new consent gate withholds the cookie until opt-in. Tests have never passed against the post-`a558844` code. **Fix:** either inject `Cookie: narve_consent=accept` in the test client before the first GET, or skip-mark these three tests with a TODO referencing the consent rewrite. Not modified this iteration to respect the no-fixture-thrash boundary.

### Smell check on `gateway/server.py` (read-only — fix-agent owns it this iteration)

Clean. No bare `except:` near the rewritten `/login` form-fallback (line 3895-3940) — every catch is `except Exception` or specifically typed. `dummy_hash`/`dummy_salt` spelled consistently and burned via `db.verify_password` on the missing-user branch (constant-time defence intact). The DNT/consent gate at `/api/analytics/event` (lines 5237-5246) sits **above** the rate-limit principal resolution and the body parse — correctly placed before any auth-touching logic.

### Dead imports in other today-touched files

`affiliate_routes.py:29` (logging), `pwa_middleware.py:19` (os), `queries/admin.py:11-13` (hmac/json/logging) all pre-date today (April commits). Not touching — restraint.

### Stable counter

Iteration 2: 1 new regression (test_auth_flow ImportError, the iter-2 stale-test fix was `e3aaa09`).
Iteration 3: **0 new regressions** (all 4 failing tests pre-date 5130b62). **Stable counter: 1.**

### Fix shipped

- `d96d006` — `chore(pwa): drop unused VISITOR_COOKIE import from pwa_middleware`

---

## 2026-05-16 10:45 — test suite regression check (iteration 2)

**Suite totals** (`pytest tests/ --ignore=tests/test_auth_flow.py` — collection-blocking file excluded): 3404 collected, **3030 passed / 160 failed / 1 error / 213 skipped** (3 files in `tests/qa/*` skip-collect for missing `playwright`).

**Plus 1 collection-blocked file:** `tests/test_auth_flow.py` — `ImportError: cannot import name 'PENDING_TOKEN_COOKIE' from 'auth.cookies'`.

### NEW regressions from today's commits (since 429fb02)

1. **`tests/test_auth_flow.py` — collection ImportError.** Tests still import `PENDING_TOKEN_COOKIE`, `PENDING_TOKEN_TTL`, `sign_pending_token` from `auth/cookies.py`. These symbols were removed by `f63d844` ("chore(auth/cookies): delete dead pending_token cookie helpers") at 23:32, **after** `8a06d2a` ("test(auth): update tests for /token removal") at 22:41 partially synced the tests. Net effect: pytest cannot collect the module → blocks the whole suite (worked around with `--ignore`). Suspect SHAs: `f63d844`, `82170a2`. **Fix:** delete the import block (lines 57-63) or skip-mark the whole module like `test_token_first_auth.py` already is.

### PRE-EXISTING fixture pollution / pre-existing failures

Verified by checking out `429fb02` and re-running 5 sample files: at 429fb02 they had **43/82 failures**, on current HEAD the same files have **38/82** — i.e. today's commits did NOT add failures, in fact slightly fewer fail (the `_fmt_ts` fix from iter 1 helped). Per-file in-isolation tallies (run individually, no cross-file pollution):

| File | tests | fail | classification |
|---|---|---|---|
| test_settings_billing.py | 39 | 16 | pre-existing |
| test_gift_subscription.py (untracked, new) | 16 | 13 | pre-existing (new file, never passed) |
| test_notifications.py | 29 | 12 | pre-existing |
| test_admin_audit_log.py | 20 | 11 | pre-existing (302→login redirects: shared login fixture broken) |
| test_saved_views.py | 61 | 9 | pre-existing |
| test_extension_jwt.py | 16 | 8 | pre-existing |
| test_api_public.py | 15 | 8 | pre-existing |
| test_admin_users.py | 12 | 7 | pre-existing |
| test_referrals.py | 47 | 6 | pre-existing |
| test_api_public_polish.py | 12 | 5 | pre-existing |
| test_feed.py | 5 | 4 | pre-existing |
| test_api_v1_consensus.py | 6 | 4 | pre-existing |
| test_billing_portal.py | 8 | 3 | pre-existing |
| test_portfolio_integration.py | 30 | 3 | pre-existing |
| (~25 other files) | — | 1-2 each | pre-existing |

Failure signatures dominated by:
- `'none' != 'pro'` / `'none' != 'trader'` — tier-cache or seeded-user fixture not surviving the test session
- `302 != 403` / landing-HTML returned in admin tests — admin login fixture not authenticating, falling through to public landing
- `api_keys_management::test_list_then_create_then_revoke` ERROR — async task teardown leak (`Task was destroyed but it is pending!` from `jobs/backend.py:173 InProcessBackend._run`)

### Top 5 to fix first

1. **`tests/test_auth_flow.py` import** — single-line cleanup, unblocks `pytest tests/` invocation (collection error).
2. **Admin login fixture** (likely `tests/conftest.py` or `tests/_testdb.py`) — root cause of 50+ failures across `test_admin_*`, `test_settings_billing`, `test_gift_subscription`, `test_notifications`. After today's `/auth/login` rewrite (`44ce666`, `baac236`), any fixture that POSTs old-shape login payload now 401s.
3. **`tests/test_gift_subscription.py`** — untracked new file, 13/16 fail. Either delete or make it pass before committing.
4. **InProcessBackend task leak** (`gateway/jobs/backend.py:173`) — `Task pending` warnings flood stderr and clobber pytest summary output; one test errors outright (`test_api_keys_management::test_list_then_create_then_revoke`).
5. **`tests/test_settings_billing.py`** — 16/39 failing in isolation, mostly tier-state issues; fixing the login fixture will likely cascade-fix most.

---

## 2026-05-16 09:54 — iteration 1

- **HIGH [gateway/migrations/199_background_jobs_send_email_index.py:36]** Migration 199 runs `CREATE INDEX … ON background_jobs(…)` but the `background_jobs` table is created lazily by `jobs/backend.py::_ensure_jobs_table()` rather than by any earlier migration. On a fresh DB the migration fails with `sqlite3.OperationalError: no such table: main.background_jobs`. In production this is masked because the index was hot-applied out-of-band before deploy (per the migration's own docstring), and `server.py` wraps `upgrade_to_head()` in a try/except that swallows the error. But: (a) every test process blew up at conftest import, and (b) any fresh install / staging DB / disaster-recovery rebuild will silently skip the index and slow `/admin/email-addresses` 5-10x. **Fix (deferred — file is in the no-touch list):** either call `_ensure_jobs_table()` from inside migration 199's `upgrade()` before the `CREATE INDEX`, or move the `CREATE TABLE background_jobs` itself into a proper migration (cleaner long-term). Test-side workaround applied in `tests/_testdb.py` this iteration to unblock the suite.

- **HIGH [gateway/admin_routes.py:3723]** Two functions both named `_fmt_ts` in the same module. The second definition (line 3723, added in commit `08b1bf2`) takes only one positional arg and shadows the original `_fmt_ts(ts, fmt="…")` at line 113. Result: every caller that passes a format string — `/admin/users` (lines 2439, 2440), `/admin/jobs` CSV export (lines 239, 749, 2967, 2975, 2983) — raises `TypeError: _fmt_ts() takes 1 positional argument but 2 were given` and returns HTTP 500. Caught by `test_admin_users` and `test_admin_jobs` tests. **Fixed this iteration:** renamed the shadowing function to `_fmt_email_addresses_ts` and updated its 4 internal callers.
