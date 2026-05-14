# BUGFIX_LOG

Append-only log of real bugs fixed. Each entry records symptom, cause, fix,
and the file(s) touched. New entries at the top.

---

## 2026-05-14 — b9ecfe6: `enqueue_email` kwarg mismatch silently dropped cancellation mail

**Symptom:** Every subscription-cancellation email was silently failing in prod — customers got no notice that their plan had ended.
**Root cause:** `stripe_webhook_hardening.apply_subscription_cancelled` (~line 235) called `enqueue_email(user_id=..., template=..., context=..., tags=...)` but the canonical signature is `enqueue_email(to, template, context, ...)`. Every call raised `TypeError` and was swallowed by the surrounding try/except.
**Fix:** Resolve the user's email via `db.get_user_by_id(user_id)` and pass it as `to=`. Surface `cancel_at` / `canceled_at` / `ended_at` as `period_end_date` so the template renders a real date.
**Lesson:** Catch-all `except` around fire-and-forget jobs hides signature drift forever. Add a structured-log line on failure, not a silent swallow.
**Files touched:** `gateway/stripe_webhook_hardening.py`.

---

## 2026-05-14 — b9ecfe6: 6 email templates referenced in code but missing on disk

**Symptom:** Any code path that triggered `winback_7d`, `winback_30d`, `saved_prediction_resolved`, `weekly_intelligence`, `admin_cost_alert`, or `admin_subscription_drift` raised `FileNotFoundError` deep inside the renderer.
**Root cause:** These template names existed in call sites and the `_SUBJECTS` map but the `.html` files were never committed — missing artifact rather than logic bug.
**Fix:** Created six new templates under `gateway/email_system/templates/`, each extending `base.html` and using only variables present at the matching call site. Added the six entries to `_SUBJECTS` in `service.py` so subjects don't fall back to plain "narve.ai".
**Lesson:** A grep-for-`enqueue_email(template=`-vs-`ls templates/` CI check would have caught this years ago.
**Files touched:** `gateway/email_system/service.py`, `gateway/email_system/templates/{winback_7d,winback_30d,saved_prediction_resolved,weekly_intelligence,admin_cost_alert,admin_subscription_drift}.html`.

---

## 2026-05-14 — ebf7401: `get_invite_token` hardcoded `status = 'unclaimed'` broke login

**Symptom:** `POST /auth/login` always 401'd with "Session expired. Start again from /token." Login was unusable for any account whose invite had been claimed. Several auth-flow regressions (14 tests red).
**Root cause:** `db.get_invite_token` in `gateway/queries/auth.py` baked `AND status = 'unclaimed'` into its SELECT. Every caller (`require_pending_token`, `/register`, `/login`, `/forgot-password`) explicitly branches on `invite["status"]` for claimed / revoked cases — those branches were dead because the row never came back.
**Fix:** Drop the status filter from the SELECT. Callers already handle all three states correctly; the filter was lying about the contract.
**Lesson:** A predicate at the data-access layer that the contract above it pretends doesn't exist is worse than no predicate at all.
**Files touched:** `gateway/queries/auth.py`, `gateway/tests/test_auth_flow.py`, `gateway/tests/test_token_first_auth.py`.

---

## 2026-05-14 — f766fdb: sync `stripe.Subscription.retrieve()` inside async dep blocked event loop

**Symptom:** Subproduct-protected API requests for non-Pro users (on cache miss) blocked the FastAPI event loop ~150-500 ms each. Concurrent requests queued behind the Stripe round-trip; p99 latency miserable.
**Root cause:** `gateway/subproduct_access.py:177` called the synchronous Stripe SDK directly from an async dependency — every coroutine on the loop stalled for the full network round-trip.
**Fix:** Convert `_live_stripe_status` to async, wrap the SDK call in `asyncio.to_thread()`. Bumped the verify cache TTL from 60s to 300s so cold-cache hits are 5x less frequent.
**Lesson:** Any sync I/O inside an async route is a load-bearing tax on every other in-flight request. Grep for `stripe.` / `requests.` / `urllib` from inside async defs at audit time.
**Files touched:** `gateway/subproduct_access.py`.

---

## 2026-05-14 — f766fdb: sync `stripe.checkout.Session.create()` inside async POST blocked event loop

**Symptom:** Same shape as above — subproduct signup / checkout POST handlers blocked the loop for the duration of the Stripe round-trip.
**Root cause:** `gateway/subproduct_signup_routes.py:117` called `stripe.checkout.Session.create()` synchronously inside an async handler.
**Fix:** Convert `_build_checkout_session` to async and wrap the SDK call in `asyncio.to_thread()`.
**Lesson:** Same as the Subscription.retrieve case above — there is no "just one place"; treat sync-in-async as a class of bug, not a one-off.
**Files touched:** `gateway/subproduct_signup_routes.py`.

---

## 2026-05-14 — fed4f51: N+1 query in `/embed/best-bets` — 61 queries per request

**Symptom:** Public embed endpoint hit the DB ~61 times per request (1 list + N detail queries). Cache-hostile, scraper-vulnerable, slow under load.
**Root cause:** The route iterated a list of prediction ids and issued a separate SELECT for each — textbook N+1.
**Fix:** Collapse to a single `SELECT ... WHERE id IN (...)` query, then in-memory join. Added a 120-second cache layer on the response since this endpoint is public and idempotent.
**Lesson:** Public unauthenticated endpoints are the cheapest place for an attacker to spend our DB budget. Cap query count + cache hard.
**Files touched:** `gateway/embed_routes.py`.

---

## 2026-05-14 — dbe9692: stale `gateway/requirements.lock` pinning CVE-vulnerable cryptography 44.0.1

**Symptom:** Lock-file duplication meant the production deploy was pulling cryptography 44.0.1, which has unpatched CVEs CVE-2026-26007, CVE-2026-34073, CVE-2026-39892.
**Root cause:** A historical refactor moved the lockfile to repo-root `requirements.lock` but left `gateway/requirements.lock` behind. The gateway Dockerfile still preferred the gateway-local one.
**Fix:** Delete `gateway/requirements.lock`. Repo-root `requirements.lock` is the single source of truth and pins cryptography to a patched version.
**Lesson:** Duplicate lockfiles are worse than no lockfile — they ship the older one silently.
**Files touched:** `gateway/requirements.lock` (deleted), `requirements.lock`.

---

## 2026-05-14 — fff85c9: whale/centralbank/world-health trusted gateway headers without HMAC

**Symptom:** Three new subproduct dashboards (whale, centralbank, world-health) bound `0.0.0.0` and trusted `X-Gateway-User-Id` / `X-Gateway-User-Email` headers verbatim. Anything on the same network — or anyone who reached the host before the firewall was right on deploy — could impersonate any subscriber.
**Root cause:** New servers scaffolded from a template that predated the voters-dashboard HMAC hardening pattern.
**Fix:** Each `server.py` now runs an HTTP middleware that rejects non-healthcheck requests whose `X-Gateway-Secret` doesn't match `GATEWAY_SSO_SECRET` via constant-time compare. Bind defaults to `127.0.0.1` so the gateway on the same host is the only ingress.
**Lesson:** Scaffold templates carry forward both good and bad. Promote a hardened reference (voters-dashboard) and copy from it, not from the next-newest sibling.
**Files touched:** `centralbank-dashboard/server.py`, `whale-dashboard/server.py`, `world-health-dashboard/server.py`.

---

## 2026-05-14 — 5460fa4: CSRF `/api/scraper/` prefix exempted every scraper subroute

**Symptom:** `_CSRF_EXEMPT_PREFIXES` contained `/api/scraper/`, so every POST under that prefix bypassed CSRF — past, present, and any future subroute would inherit the bypass silently.
**Root cause:** Prefix-based exemption was a convenience that became a footgun. Only `/api/scraper/ingest` (which uses `X-Scraper-API-Key`) needs the bypass.
**Fix:** Move `/api/scraper/ingest` to `_CSRF_EXEMPT_PATHS` (exact match). Prefix list is now empty. Regression test asserts a non-listed sibling sub-path is NOT exempt.
**Lesson:** Exempt by exact path, never by prefix. Future routes should fail closed.
**Files touched:** `gateway/security/csrf.py`, `gateway/tests/test_csrf.py`.

---

## 2026-05-14 — 5460fa4: `set_user_role` didn't invalidate per-user cache — role-change leak risk

**Symptom:** A user demoted from admin to free still saw the admin-cached payload of `/dashboards`, `/settings`, `/signal-search`, `/sources/{handle}` until their cache key expired. Today's payloads don't contain admin-only fields, so no live leak — but the write path was lying.
**Root cause:** `set_user_role` updated the row + revoked sessions, but didn't touch the per-user async cache surfaces added in 463384e.
**Fix:** Add `on_role_change(user_id)` to `cache/ttl.py` (mirror of `on_subscription_change`). `set_user_role` now fires it after the UPDATE. Two unit tests cover: sync TTL cache untouched; other users' keys survive.
**Lesson:** Every write that changes an authorization-relevant field needs a cache-bust hook in the same function. Make it the function's responsibility, not the caller's.
**Files touched:** `gateway/cache/ttl.py`, `gateway/queries/auth.py`, `gateway/tests/test_cache.py`.

---

## 2026-05-14 — 38a6593: voters CSP `unsafe-inline` in script-src was dead permission

**Symptom:** voters-dashboard CSP included `'unsafe-inline'` in `script-src` even though no inline `<script>` blocks existed (frontend JS already lives in `/static/app.js`). Dead permission, bad shape — would mask any future inline injection.
**Root cause:** Template-inherited permission that nobody pruned when the inline script was extracted.
**Fix:** Drop `'unsafe-inline'` from `script-src`. Add `base-uri 'self'` and `form-action 'self'` to match the gateway's hardened CSP shape. `style-src` keeps `'unsafe-inline'` (lower-risk; voters has inline `style=` attributes on the timeline strip).
**Lesson:** CSP audits should diff the policy against what the page actually uses. Permissions you don't need are attack surface you can't see.
**Files touched:** `voters-dashboard/server.py`.

---

## 2026-04-23 — Regression sweep (REGRESSION_SWEEP.md)

### 17a. `forecast_sync` nightly crash — `no such column: close_at`

- **Symptom:** the daily `forecast_sync` job (cron 03:15 UTC) has been the
  only consistently-failing job on the production server. Every run raised
  `sqlite3.OperationalError: no such column: close_at` and produced zero
  external-forecast matches until the next run failed the same way.
- **Cause:** a prior refactor renamed the relevant column in
  `market_snapshots` from `close_at` → `close_time`. Everywhere else
  followed except this job's SQL.
- **Fix:** `gateway/jobs/forecast_sync.py:147` — use
  `MAX(close_time) AS close_at`. The alias preserves the public result-dict
  shape so downstream matcher code (which reads `row["close_at"]`) keeps
  working. Inline comment added so the next rename pass catches it.
- **Files:** `gateway/jobs/forecast_sync.py`.
- **Detection:** `SELECT job_name, COUNT(*) FROM job_runs WHERE ok=0 AND started_at > strftime('%s','now','-1 day')` — one hit, one pattern.

### 17b. `admin_routes.register()` crash — `NameError: 'backups_page' is not defined`

- **Symptom:** server startup logged
  `ERROR  admin_routes.register failed: name 'backups_page' is not defined`
  and the admin-routes import block aborted after line 1494.
  Every admin route REGISTERED AFTER that line was silently dropped —
  `/admin/backups` didn't exist, and nothing after it in the `register()`
  function got mounted either.
- **Cause:** a parallel agent wired the `/admin/backups` registration but
  never committed the handler.
- **Fix:** the real `backups_page` handler landed via a parallel session
  (`gateway/admin_routes.py:878`). Verified this pass that the route is
  now in `app.routes` and the caught-exception block no longer fires on
  cold boot.
- **Files:** `gateway/admin_routes.py` (no edit needed in this session —
  parallel agent's fix verified).

---

## 2026-04-23 — Edge-case hardening follow-up (local-only commit, no deploy)

Scope: wire the `security/input_hygiene.py` + `security/idempotency.py`
modules shipped earlier the same day into the specific handlers flagged
as ⚠️ pending in `EDGE_CASES.md`. No new modules; no schema changes.

### `/api/portfolio/kalshi/connect` — double-login on retry

- Symptom: a retry within 10 s (flaky network + "Connect" button
  without a loading state) fired two full Kalshi login calls. Kalshi
  rate-limits aggressively, so the second call often returned 429
  and the user saw "Connect failed" even though the first call had
  already stored a valid token.
- Cause: no idempotency at the narve→Kalshi edge.
- Fix: wrap the login + upsert in `with_idempotency(op="kalshi_connect")`
  keyed on `Idempotency-Key` header or the submitted email (never the
  password — logging hygiene). 10 s TTL. Second call replays the
  cached response without re-calling Kalshi.
- Files: `gateway/portfolio/routes.py`.

### `/settings/billing/cancel` step=3 — duplicate winback emails

- Symptom: double-submit of the final cancel step queued the winback
  email sequence twice. A user cancelling with "Back" + "Continue"
  typing too fast would receive 6–10 emails over the next 30 days
  instead of 3–5.
- Cause: `_queue_winback_emails(user_id, email)` is a fan-out; no
  idempotency.
- Fix: wrap the step=3 branch in `with_idempotency(op="billing_cancel_finalize")`
  keyed on `attempt_id`. 30 s TTL (longer than other ops because the
  email-queue insert takes a moment; catches the slow-click-as-retry
  case).
- Files: `gateway/billing_routes.py`.

### `/settings/billing/addon` — period_end shifted forward on retry

- Symptom: clicking "Add trading add-on" twice pushed `period_end`
  forward by 60 days total instead of 30. Rare but real — users get a
  free 30-day top-up accidentally, which quietly costs us.
- Cause: `set_trading_addon(True, period_end=now + 30*86400)` is
  additive-looking but not keyed on idempotency.
- Fix: `with_idempotency(op="billing_addon_add")` keyed on the addon
  name. 10 s TTL.
- Files: `gateway/billing_routes.py`.

### `/api/saved` + `/api/sources/following` — no pagination

- Symptom: a user who had saved 5 000+ predictions got the entire
  list in one response — 2–3 MB, ~800 ms backend time. The "Saved"
  tab stuttered on mobile.
- Cause: both endpoints pulled the full list from the DB helper and
  returned it flat.
- Fix: wire `clean_page` / `clean_per_page` (defaults 50 / 100,
  caps 200 / 500) and return the canonical
  `{items, total, page, per_page, pages}` envelope. Keeps the
  existing `count` field for backwards compatibility with older
  clients.
- Files: `gateway/server_features.py`.

### `/api/feedback` — bespoke input validation missed control chars

- Symptom: pasting text that contained zero-width joiners or BOM
  produced feedback rows that looked identical to legitimate rows
  but hashed differently — duplicate-detection + search-exact missed
  them. Null-byte pastes triggered 500 downstream at the DB layer.
- Cause: handler used `(title or "").strip()[:200]` which doesn't
  normalise unicode or reject control chars.
- Fix: route title + body through `clean_text(..., required=True)`
  with per-field `max_len`. NFC normalises; zero-width / bidi
  stripped; null / C0 rejected with a clean 400 carrying
  `{"error", "field"}`.
- Files: `gateway/feedback_routes.py`.

### Regression tests for the above

- Added 6 integration-level tests to `tests/test_edge_cases.py`:
  `TestWiredFeedbackEndpoint`, `TestWiredPortfolioKalshi`,
  `TestWiredBilling` (× 2), `TestWiredPaginationHelpers` (× 2). Each
  inspects the source of the wired handler and fails loudly if an
  agent accidentally unwires the hygiene / idempotency / pagination
  layer.
- Full suite: **60 passing**.
- Files: `gateway/tests/test_edge_cases.py`.

---

## 2026-04-23 — Edge-case hardening sweep

Scope: defensive coverage across the 13-item input matrix, pagination
boundaries, idempotency for subscription-critical writes, and timezone
sanity. Companion docs: [`EDGE_CASES.md`](EDGE_CASES.md),
[`SUBSCRIPTION_STATE_MACHINE.md`](SUBSCRIPTION_STATE_MACHINE.md).

### Input drift → 500 instead of 400

- Symptom: a handler receiving `NaN`, `Infinity`, a decimal-shaped
  string where an int was expected, or a 10 k-char zalgo stream
  typically returned a 500 from an uncaught `ValueError` / `TypeError`
  downstream (pydantic coerces many things, but not all).
- Cause: no shared normaliser layer; every handler rolled its own
  `int(...)` or `str.strip()` guards, and some skipped them entirely.
- Fix: added `gateway/security/input_hygiene.py` with six pure helpers
  (`clean_text`, `clean_int`, `clean_float`, `clean_email`,
  `clean_handle`, `clean_page`, `clean_per_page`). Every input now
  flows through one of them and returns a predictable 400 with a
  `{error, field}` body.
- Files: `gateway/security/input_hygiene.py` (new),
  `gateway/tests/test_edge_cases.py` (new).

### Double-click "Subscribe" → duplicate Stripe subscription

- Symptom: users who impatiently double-clicked on Checkout, or whose
  mobile client retried the POST after a network blip within 10 s,
  sometimes ended up with two parallel Stripe subscriptions.
- Cause: no idempotency layer for subscription-critical writes. The
  Stripe-webhook ledger (`processed_stripe_events`) covers Stripe →
  narve duplicates but not narve → Stripe.
- Fix: added `gateway/security/idempotency.py` with an
  `Idempotency-Key`-aware helper (`with_idempotency`) and a JSON-body
  fingerprint fallback when the header is missing. 10 s TTL window,
  Redis when available, in-process otherwise. Module ships; wiring
  into billing / kelly / portfolio handlers is a follow-up PR so each
  integration gets a code-review pass.
- Files: `gateway/security/idempotency.py` (new).

### DST transition unprotected

- Symptom: unit tests didn't assert physical-time math across
  `Europe/Berlin` spring-forward; a regression could silently inflate
  elapsed-time deltas by an hour for any metric computed on wall-clock
  subtraction.
- Cause: no explicit regression. Every datetime storage path uses
  integer epochs, but no one had checked.
- Fix: `TestTimezone.test_dst_transition_day` asserts physical delta
  across 2026-03-29 Europe/Berlin spring-forward is bounded below the
  wall-clock gap.
- Files: `gateway/tests/test_edge_cases.py`.

### Pagination abuse

- Symptom: a mobile client (or malicious scraper) sending
  `per_page=10000` got a 10 k-row response; `page=-1` produced a 500
  from negative `OFFSET`.
- Cause: per-endpoint clamping existed but wasn't uniform — some
  capped, some passed through.
- Fix: `clean_page` / `clean_per_page` helpers (defaults 20 / cap 100
  / max_page 10 000). Collapse negative / zero / non-numeric input to
  defaults; clamp over-cap values silently (better UX than a 400 for
  a mobile app that hasn't heard about the limit).
- Files: `gateway/security/input_hygiene.py`,
  `gateway/tests/test_edge_cases.py` (`TestPaginationBoundaries`).

### Unicode / zero-width / bidi smuggling

- Symptom: a username with zero-width joiners looked identical to an
  existing one; a bidi-control character could reverse the apparent
  meaning of a displayed handle.
- Cause: no normalisation step before length or charset checks.
- Fix: `clean_text` applies NFC normalisation, strips
  zero-width / BOM / bidi-control glyphs, then enforces length in
  code points.
- Files: `gateway/security/input_hygiene.py`.

---

## 2026-04-21 — Security audit #2 follow-up

Scope: CRITICAL/HIGH items from `gateway/NARVE_SECURITY_AUDIT.md` audit #2.
No new features. Fix-only session.

### CRITICAL #1 — RCE via `eval()` on Polymarket Gamma response

- Symptom: `gateway/jobs/resolution_jobs.py:80` fell back to
  `eval(resolved_prices)` whenever Polymarket returned `outcomePrices`
  as a string instead of a list. Polymarket historically does that
  (JSON-encoded `"[0.65, 0.35]"`), so the path runs for every resolved
  market with price-based settlement.
- Cause: `eval()` executes arbitrary Python. A crafted upstream payload
  (accidental server misbehaviour or TLS downgrade / MITM) becomes
  unsandboxed RCE as the job-worker user — full DB, Kalshi tokens,
  Stripe webhook secret.
- Fix: Replace `eval()` with `json.loads()`. Malicious input raises
  `ValueError`, which the existing `except Exception` already handles.
  Added explicit `(ValueError, TypeError, IndexError)` tuple so we
  don't silently swallow genuine bugs elsewhere in the branch.
- Files: `gateway/jobs/resolution_jobs.py`, `gateway/tests/test_resolution_polling.py`.
- Regression tests: 4 new tests in `TestOutcomePricesParsingIsSafe`,
  including a sentinel check that the parser does not execute
  `__import__("builtins")._pwned_by_resolution_eval = True`, plus a
  source-grep assertion that the file no longer contains a bare
  `eval(` call.

### HIGH #1 — `/auth/logout` had no rate limit

- Symptom: `POST /auth/logout` accepted unbounded requests from a
  single IP. Low-impact DoS (spam the security event log, burn CSRF
  cycles).
- Cause: Omission. Every other auth endpoint has an inline
  `_is_rate_limited` guard.
- Fix: Added per-IP limit (20 req/min), returns 429 with `Retry-After: 60`.
  Throttled responses still clear the client-side session cookies so a
  legitimate user caught in a spam storm isn't left holding a stale
  session.
- Files: `gateway/server_features.py`, `gateway/tests/test_logout.py`.
- Regression tests: `TestLogoutRateLimit` — 2 tests verifying the 21st
  request from a single IP returns 429 and still clears cookies.

### HIGH #2 — Dynamic `ORDER BY` without explicit allowlist

- Symptom: Two call sites interpolated a variable into `ORDER BY`:
  - `gateway/db_referrals.py:437` — `col` from `get_leaderboard(period)`
  - `gateway/queries/watchlist.py:92` — `order` from `list_saved_predictions(sort)`
  Both are already dict-bounded today (the input parameter keys a small
  dict; anything else defaults to a known-safe value). But the safety
  is implicit and a future refactor that forwards a query-param verbatim
  into the dict lookup would silently regress into SQL injection.
- Cause: Missing defence-in-depth. The contract isn't visible at the
  interpolation site.
- Fix: Added module-level `frozenset` allowlists and an explicit
  `if col not in _ALLOWED_…: raise ValueError(...)` guard before the
  f-string SQL build.
- Files: `gateway/db_referrals.py`, `gateway/queries/watchlist.py`,
  `gateway/tests/test_referrals.py`.
- Regression tests: `test_leaderboard_sort_column_is_allowlisted`
  asserts the allowlist set and that every documented period value
  resolves to an allowlisted column.

### HIGH #5 — Open-redirect review (no code change — false positive)

- Symptom: Audit flagged 12 `RedirectResponse` calls with variable
  destinations as potentially open-redirect.
- Cause: Hand-audited each site (`server.py:1028/6028/6035/6040`,
  `admin_routes.py:186/406`, `status_routes.py:525–620`,
  `subproduct_signup_routes.py:215`). Every variable target is either
  an apex from `_request_apex()` (which rejects anything not in
  `ALLOWED_DOMAINS`), an integer incident_id from a path parameter, a
  dict-bounded `key`, a regex-validated feature-flag key, or the
  hosted Stripe checkout URL. No exploitable open-redirect exists.
- Fix: Documented in this log; no code change. If this changes
  (someone adds a raw request-param-sourced target), the audit's next
  run will catch it.

### Defence scans — clean

- `sqlite3.Row.get()` misuse: 79 hits across 12 files, every one on
  `dict(row)` conversions, JSON API payloads (Kalshi, Polymarket,
  FEC, SEC), or inside a polymorphic helper that handles Row and dict.
  No broken Row.get() calls.
- TODO/FIXME scan: 10 markers, all deferred-feature markers that
  pair up with audit-acknowledged deferrals (C4 per-user tokens, C8
  Kalshi encryption, C9 EIP-191 wallet signature, plus pending
  Stripe add-on checkout UX). None are stale bug notes — all have
  clear context referencing audit items.

### Deferred CRITICAL/HIGH (not this session)

- **CRITICAL #2** — pin-upgrade the 14 CVEs (cryptography 42.0.8,
  starlette 0.37.2, python-multipart 0.0.18, pillow 11.3.0, etc.).
  Audit itself recommended a dedicated PR because the upgrade crosses
  the Fernet backend and Starlette middleware stack; touching it in
  this fix-only session would be scope creep and risk a one-shot
  rollback becoming difficult.
- **CRITICAL #3** — at-rest encryption of `kalshi_token` column plus
  data migration. The migration is non-trivial (needs
  `CREDENTIALS_ENCRYPTION_KEY` present, has to rewrite every existing
  plaintext row atomically, and every read site has to switch to
  `decrypt_token()`). Parks as the next security-focused PR.
- **HIGH #3** — 5 admin/management endpoints missing explicit
  `@rate_limit` decorators. Scope-bounded but touches several routes;
  next fix-only pass.
- **HIGH #4** — 7 billing/subscription endpoints missing per-user
  rate limits. Same rationale as H3.
- **HIGH #6** — 9h stash review on `feature/referral-program`.
  Procedural, not a code fix. Operator action.
- **HIGH #7** — `auth.db` filesystem permissions. Operator action on
  the server.
- **HIGH #8** — Lock `requirements.txt` to a `requirements.lock.txt`.
  Deploy-process change, not a code fix.

### Test run posture

- Resolution, logout, referrals, status, auth-flow suites run green
  for the tests that exercise the touched code.
- Pre-existing failures observed but not in scope for this pass:
  - `tests/test_account_deletion.py::TestSoftDelete::test_deletion_revokes_sessions`
    segfaults Python 3.9 — a bus error in the DB cleanup path, not in
    any code this session touches.
  - 10 pre-existing failures in `tests/test_auth_flow.py` (suspended
    account, token-claimed register flow, pending-token cookie clear
    on login). Sibling-session drift; none relate to logout / resolution /
    leaderboard.

Both items predate this session and are documented so the next fix-pass
picks them up; they are not my regressions.
