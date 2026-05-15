# Adversarial audit — `gateway/admin_jobs_routes.py`

Scope: 6 routes registered by this module (1 HTML page + 5 JSON endpoints, 3 mutating). Cross-referenced with `gateway/server.py` (auth + CSRF middleware), `gateway/scheduler/scheduler.py`, `gateway/queries/jobs.py`, `gateway/security/rate_limiter.py`.

Threat model: external attacker (unauth), authenticated non-admin user, compromised admin credential, on-path proxy abuse, host-header / origin spoofing.

## Severity tally

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 3 |
| Low      | 5 |
| Info     | 2 |

The file is in good shape on the core asks. Every mutating route calls `server._require_admin_user(request)`, the global `CSRFMiddleware` (registered in `server.py:1332`) enforces double-submit on every POST under `/admin/api/jobs/...` (path is NOT in `_CSRF_EXEMPT_POSTS` nor any `_CSRF_EXEMPT_POST_PREFIXES`), and the job-`name` flows into APScheduler as an opaque identifier (no exec / no shell / no SQL string interpolation). The findings below are real defects but none of them grant RCE or full auth bypass on their own.

---

## Top 10 findings (severity-sorted)

### 1. [MEDIUM] No explicit job-name allowlist — admin can trigger/pause any APScheduler id, including malformed ones

`admin_api_job_trigger`, `_pause`, `_resume`, and `_history` accept `name: str` straight from the URL path with no validation, character class, or length cap, then pass it to `sched.trigger_now(name, ...)` → `self._impl.modify_job(name, next_run_time=...)` (`scheduler/scheduler.py:271-279`).

The trigger-now path also writes the name into `self._pending_trigger_reasons[name]` keyed on attacker-controllable input. A malicious admin can:

- Trigger arbitrary in-process jobs (including ones registered by other modules) — currently bounded by the set the scheduler has registered, but with no allowlist this surface grows silently every time someone adds an `add_cron`/`add_interval` call elsewhere.
- Populate `_pending_trigger_reasons` with thousands of unbounded-length names, since the dict is never pruned for unknown names — APScheduler's `modify_job` raises `JobLookupError`, which is caught by the route's `except Exception` and surfaces as a 404, BUT the dict-set on line 278 happens BEFORE the `modify_job` call, so the dict entry persists forever. This is a slow in-process memory leak weaponizable by a compromised admin credential (rate-limited to 30/min, but the dict entry is permanent — 30/min × an 8h shift = 14,400 stale keys with arbitrary-length string values).

**The module docstring claims** ("job-name allowlist (prevents arbitrary code execution via cron name)") that one exists; in practice the only "allowlist" is whatever APScheduler happens to have registered. There is no explicit `if name not in REGISTERED_JOBS: raise 400` check.

Fix: validate `name` against `sched.jobs.keys()` BEFORE the `_pending_trigger_reasons[name] = ...` assignment; reject with 404 if not in the registry.

File: `gateway/admin_jobs_routes.py:285-294`; root cause `gateway/scheduler/scheduler.py:271-279`.

### 2. [MEDIUM] PATCH/PUT/DELETE CSRF is in soft-warn mode globally (`CSRF_PATCH_DELETE_ENFORCE=false` by default)

Not specific to this file but materially relevant: the module's docstring claims "POSTs enforce the global CSRF middleware — no exemption". That is true for POST. But the platform's CSRF middleware (`server.py:1213-1215, 1300-1313`) defaults `CSRF_PATCH_DELETE_ENFORCE` to false in Phase-1 rollout — failed CSRF validation on PATCH/PUT/DELETE only logs a warning, the request still completes.

Today this file uses only POST for mutations, so no live exposure. But if someone later refactors `/pause`/`/resume`/`/trigger` to PATCH (the more REST-idiomatic verb for "modify scheduled state"), the routes will silently lose CSRF protection until the rollout flag is flipped. Add a regression test asserting these three remain POST until Phase 2 is on.

File context: `gateway/server.py:1244-1313`.

### 3. [MEDIUM] `_admin_key` rate-limit bucket leaks admin user-IDs to anonymous attackers

`_admin_key()` (lines 38-42) returns `f"admin_jobs:{user['user_id']}"` for authenticated admins and `f"admin_jobs:anon:{ip}"` otherwise. The rate-limit decorator runs BEFORE `_require_admin_user` (decorator order in Python is bottom-up: `@rate_limit` wraps the inner function which then calls `_require_admin_user`). For an unauth request, `current_user(request)` returns `None`, so `_admin_key` returns the anon key — fine.

But the decorator's response, when limit is hit, includes `X-RateLimit-Limit`/`X-RateLimit-Remaining` headers. Combined with the fact that the limit (300, 120, 60, 30) is route-specific and the bucket is per-`user_id` for admins, a stolen-session adversary who can observe response headers can confirm whether the cookie's user is admin (300/min bucket on `/refresh`) vs. anon (also 300/min — same number, so this is actually OK on observation, but the existence of TWO independent buckets per route — admin vs anon — means an attacker who can flood the anon bucket cannot DoS the admin bucket; conversely an admin enumerating user_ids cannot starve other admins because their buckets are user-scoped). Reviewed and this is largely fine, but the lack of consistent admin-only gating (route returns 403 before rate-limit body in some paths and after in others) deserves a single test asserting the order.

Real concern: when `_require_admin_user` raises `HTTPException(403)` for a non-admin authenticated user, the rate-limit decorator has ALREADY consumed a slot from the user's `admin_jobs:<user_id>` bucket. A non-admin who knows the routes exist can burn 30 trigger-attempts/min against their own user-id bucket — annoying but harmless. Note for future: gate the rate-limit decorator behind admin-only check, or fall through to `anon:<ip>` for non-admins.

File: `gateway/admin_jobs_routes.py:38-42`.

### 4. [LOW] `triggered_by` is hard-coded to `"admin"` — no audit trail of WHICH admin triggered a job

`sched.trigger_now(name, triggered_by="admin")` (line 291) writes `"admin"` into `job_runs.triggered_by`. The acting admin's `user_id`/`email` is never persisted. If a compromised admin credential triggers a job that does damage (e.g., the `claude_cost_daily_09utc` or any cron that posts to external APIs), incident response cannot determine WHICH admin clicked the button.

Fix: thread the admin's `user_id` into `triggered_by` as `f"admin:{user_id}"` or add a separate `triggered_by_admin_id` column. The audit-row dict in `list_recent_job_runs` already returns `triggered_by` to the UI, so this would be a one-line plumbing change.

File: `gateway/admin_jobs_routes.py:285-294`.

### 5. [LOW] `/admin/api/jobs/{name}/history` SQL filters job_name with parameter — but no name validation on input

`admin_api_job_history` (lines 247-258) parameterizes the query correctly (`WHERE job_name = ?`), so this is NOT SQLi. However, the route happily accepts a 100KB `name` in the URL path (FastAPI's path params have no built-in length cap), runs the indexed SELECT, returns an empty result, and the whole roundtrip costs a SQLite query for every garbage name an attacker (with admin creds) feeds it. Compounds with the rate-limit budget — 60 trash queries/min/admin.

Marginal real impact, but combined with finding #1 the lack of an allowlist is a small DoS amplifier. Add `if name not in known_names: raise HTTPException(404)` and cache `known_names` for ~30s.

File: `gateway/admin_jobs_routes.py:247-258`.

### 6. [LOW] `_render_recent_rows` truncates error messages to 200 chars but escapes correctly — only a UI smell

Line 174: `_esc(str(err)[:200])`. The escape happens AFTER slicing. If `err` ends mid-multi-byte sequence the slice could produce a partial UTF-8 sequence; `html.escape` then re-encodes safely so the browser never crashes, but the cell shows a mojibake suffix. Slice on chars not bytes (Python `str` slicing IS char-based, so this is actually safe — included only because errors are attacker-influenced data via the wrapped-function exception path and the chain deserves a comment). No exploit.

File: `gateway/admin_jobs_routes.py:172-176`.

### 7. [LOW] `_job_stats` swallows ALL exceptions and returns `{}` — a malformed `job_runs` row produces silent zeroes

Lines 56-79 wrap the entire stats SELECT in `except Exception`. Any operational problem (corrupted SQLite, locked DB, schema drift) renders the legacy `/admin/api/jobs` route as "everything is zero, all jobs look healthy". An admin investigating an outage may misread this as "all clear" rather than "monitoring is dead". Same anti-pattern at lines 80, 175-177, 263, 314-318. Log → metrics is fine; SILENCE is not.

Fix: return an explicit `{"error": "stats unavailable"}` payload (or 5xx) on `/admin/api/jobs/refresh` so the UI can show a banner instead of "0 runs in window".

File: `gateway/admin_jobs_routes.py:56-79`, `gateway/queries/jobs.py:80-82, 175-177, 263`.

### 8. [LOW] Trailing-slash sister routes — FastAPI normalizes but worth confirming for `_CSRF_EXEMPT` near-misses

FastAPI by default does NOT redirect `/admin/api/jobs/{name}/pause/` → `/admin/api/jobs/{name}/pause` unless `redirect_slashes=True` (the default). Worth a one-line test verifying that `/admin/api/jobs//pause` (empty name) returns 404 cleanly rather than triggering an empty-string job lookup that the scheduler's `modify_job` might handle as "all jobs" in some APScheduler versions. Confirmed by reading apscheduler — `modify_job(job_id=None)` raises, `modify_job(job_id="")` raises JobLookupError. Safe today.

File: `gateway/admin_jobs_routes.py:261, 273, 285`.

### 9. [LOW] No `Cache-Control: no-store` on `/admin/api/jobs/refresh` JSON — admin job status may be cached by browser/CDN

The route returns a `JSONResponse` with no cache directives. Behind Cloudflare with the WAF rule mentioned in `gateway/CLOUDFLARE_CHANGES.md` the path is admin-gated, but a misconfigured intermediate proxy or a browser back-button could surface stale job-run data. Add `headers={"Cache-Control": "no-store, private"}` to all five JSON routes.

File: `gateway/admin_jobs_routes.py:198-215, 220-244, 247-258`.

### 10. [INFO] Module docstring claims allowlist exists; implementation relies on APScheduler's registry as the de-facto allowlist

The docstring at lines 1-17 promises three guarantees: admin gate, CSRF on POSTs, and (implicitly via the wording) protection against arbitrary code via cron name. Finding #1 above shows the third guarantee is incidental rather than explicit. Either implement the explicit allowlist or rewrite the docstring to describe the actual contract ("`name` is dispatched to APScheduler.modify_job; unknown names → 404"). Docs that lie are worse than missing docs.

File: `gateway/admin_jobs_routes.py:1-17`.

### Extra [INFO] (#11, beyond top 10): Late local imports inside hot paths

`from scheduler import scheduler as sched` is repeated inside every mutating handler (lines 225, 265, 277, 289). This is intentional per the scheduler module's commentary (avoid load-order coupling at import time), but it adds an `sys.modules` lookup per request. Trivial perf cost; flag only because the same pattern shows up four times — wrap in a single module-level helper to avoid drift if the import path ever changes.

---

## What this file does well

- Admin guard on every route (HTML page guard at line 302, JSON routes at 202, 223, 250, 264, 276, 288). No route is reachable without `is_admin`.
- Rate limits scoped per-admin (not per-IP), so a stolen credential cannot be multiplied by IP rotation.
- HTML rendering escapes every dynamic value through `_esc()` → `html.escape()` (lines 85-86).
- SQL uses parameter binding throughout (`queries/jobs.py:71, 78, 114, 156, 226`); no string concat.
- Mutating verbs are exclusively POST, anchored under `/admin/api/jobs/...` which is NOT in any CSRF exemption list.
- `job_name` is never `exec()`'d, never shell-passed, never used as a filename — it's only ever a dict key + APScheduler job id + SQL parameter.
- 2FA redirect is handled (line 305-306).

---

## Quick-fix priorities

1. Add explicit `name in sched.jobs` allowlist guard before `_pending_trigger_reasons[name] = ...` in `scheduler.trigger_now` (resolves #1).
2. Plumb the acting admin's id/email into `triggered_by` (resolves #4).
3. Surface backend errors in `/refresh` instead of returning fake zeroes (resolves #7).
4. Add `Cache-Control: no-store` to all five JSON responses (resolves #9).
5. Rewrite the module docstring to match reality (resolves #10).

All five are <30-line patches; none requires schema changes.
