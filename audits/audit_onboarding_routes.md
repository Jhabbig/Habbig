# Adversarial Audit — `gateway/onboarding_routes.py`

Auditor: Claude (Opus 4.7, 1M ctx)
Date: 2026-05-15
Scope: `/Users/shocakarel/Habbig/gateway/onboarding_routes.py` (937 lines, 21 routes)
Threats in-scope: completion-state forge, category-array validation, missing user-auth
guards, onboarding-token replay.
Out of scope: anything outside this file (CSRF middleware, session middleware,
Stripe webhook validation — those live in `server.py` / `subproduct_signup_routes.py`
and are assumed correct).

---

## Severity counts

| Severity | Count |
| --- | --- |
| CRITICAL | 0 |
| HIGH     | 0 |
| MEDIUM   | 2 |
| LOW      | 4 |
| INFO     | 3 |

---

## Top 3 (by impact × likelihood)

1. **MED-1** — Self-serve onboarding completion stamps a privileged column
   (`users.onboarding_completed`) directly from a user-controlled POST with no
   server-side gate that the user actually visited any prior step. Impact is
   contained today because no `Depends()` in the gateway gates feature access on
   that column, but the test file `gateway/tests/test_http_auth.py` documents the
   intended middleware ("Onboarding redirect from /dashboards is not wired in
   this build … re-enable when the middleware ships"). The day that middleware
   ships, this route hands every authenticated user a one-call bypass.
2. **MED-2** — `_require_admin` is called only on the two `/admin/onboarding*`
   handlers. Eighteen other routes call `_require_user(request)` which raises
   401 on no-session, but **`onboarding_page` (line 169-186) silently swallows
   any failure of `_ensure_onboarding_row` inside `try/finally` and proceeds to
   `server.render_page` for an authenticated user.** Combined with the dev
   bypass in `server.current_user` (lines 2238-2256), every localhost request
   is auto-authenticated as the synthetic dev user. If `gateway.narve.ai` were
   ever fronted by a misconfigured proxy that preserved `127.0.0.1` as the
   client host, dev-bypass + auto-row-creation = unauthenticated onboarding
   state writes. Not exploitable in current prod config but a foot-gun.
3. **LOW-1** — Category validation truncates to the first three valid entries
   *after* lower-casing and intersecting with the allow-list, which is correct
   for the category list itself, but the same JSON blob (`goals_completed`) is
   used as both a kv-cache for `categories` and an append-only audit log for
   `followed_handles`. `followed_handles` is stored verbatim from user input
   with a length cap of 10 and a `lstrip("@")` only — no character allow-list,
   no length cap per handle. A user who POSTs `handles=` with control chars or
   1 MB-each handles bloats the row to the SQLite `goals_completed` TEXT cell
   on every subsequent goal write. Self-DoS only.

---

## Findings by threat axis

### A. Completion-state forge

**MED-1** — `complete_onboarding` (lines 423-444) writes
`users.onboarding_completed = 1` and `onboarding_completed_at = now` with **no
server-side check that step_completed has actually progressed**. Any
authenticated user can `POST /api/onboarding/complete` once and instantly stamp
the privileged column. The handler also sets `user_onboarding.step_completed =
5` unconditionally — there is no `WHERE step_completed >= 4` or "categories
must be set" guard. CSRF middleware does protect this route from cross-origin
forge, so the attacker needs a valid session, but a logged-in user can self-
mark.

Today's blast radius is limited: `git grep` finds only three readers of
`users.onboarding_completed` — the column write itself, the metrics dashboard,
and one skipped middleware test
(`gateway/tests/test_http_auth.py:test_new_user_redirected_to_onboarding` is
`@unittest.skip` with the comment "re-enable when the middleware ships"). No
feature paywall, no entitlement check, no email-verification flow reads this
column. So the *forge succeeds* but doesn't unlock anything privileged.

**The bug is latent.** Comment block at line 285 of `test_http_auth.py`
says the bounce-out middleware is queued for a future sprint. The day that
middleware reads `onboarding_completed` to skip checks (e.g. "if completed,
let through to dashboards without sample-data injection") this route becomes
a one-call bypass. Recommend either (a) gating the write on a server-side
record that step_completed actually reached 4+ before issuing the stamp, or
(b) deriving `onboarding_completed` purely from `user_onboarding` row state
in the read path and never writing the user column from a user request.

**LOW-2** — `advance_step` (lines 189-203) accepts an arbitrary integer in
`step` (Form-coerced) and clamps to `0..5`, but the SQL uses
`MAX(step_completed, ?)` so a user can leap from 0 → 5 in one POST. There is
no monotonicity check that previous step's payload (e.g. `save_categories`
already wrote a `categories` key into `goals_completed` JSON) was satisfied.
Combined with **MED-1** this means a forged-complete user with `step_completed
= 5` and an empty `goals_completed` JSON looks indistinguishable from a real
finisher in `_compute_metrics`, polluting admin analytics
(`avg_time_to_complete_seconds` can be driven near zero by replaying immediate
0→5 → complete on a fresh account).

**LOW-3** — `dismiss_tour` (lines 206-220) sets `dismissed=1` and stamps
`completed_at` via `COALESCE`. A user who first POSTs `/api/onboarding/dismiss`
and *then* POSTs `/api/onboarding/complete` is recorded with both flags
positive. `_compute_metrics`'s `WHEN dismissed = 1 THEN 1` clause in the
`state_row` aggregate (line 728-734) counts the user once toward `dismissed`
*and once* toward `completed`, inflating both buckets for the same row.
Counts no longer sum to `rows_total`. Reporting-only, no auth bypass.

### B. Category-array validation

**LOW-4** — `save_categories` (lines 223-254) intersects user-supplied tokens
with `VALID_CATEGORIES` (line 69-71) and slices to 3. Good. **But the same
endpoint mirrors the result into `users.onboarding_categories` (line 247-250)
as a JSON-encoded list, which is then deserialized in
`gateway/queries/onboarding.py:39-41`.** If a future migration ever widens the
allow-list at the read site (queries) before the write site (this file), the
write here silently drops categories the rest of the system supports. Not a
bug today; it's a coupling smell — recommend a single source-of-truth constant
imported by both `onboarding_routes.py` and `queries/onboarding.py`.

**LOW-1** *(restated from Top-3)* — `follow_sources` line 331-332 caps to 10
handles total but performs only `.lstrip("@")` per handle. No length cap, no
character allow-list. `followed_handles` is then `json.dumps`-ed into
`user_onboarding.goals_completed`. Maximum per-row bloat = 10 × (request body
size cap, default 1 MB in FastAPI/Starlette unless overridden). Self-DoS
only since the row belongs to the attacker.

**INFO-1** — `suggested_sources` line 267-269 reads `?categories=` from the
query string with the same intersection guard. Identical handling. No
SQL injection — uses parameterized queries against
`source_category_credibility`. Clean.

**INFO-2** — `mark_goal` line 584 validates `key in ALL_GOALS` (a tuple of 6
constants) and returns 400 if not. Correct rejection. Path segment is bound
via FastAPI's path parameter, no manual decoding. Clean.

### C. Missing user-auth guard

**Sweep result: every mutating handler calls `_require_user(request)` on its
first line.** Verified by manual scan of all 21 handlers:

- `onboarding_page` (170) ✓
- `advance_step` (190) ✓
- `dismiss_tour` (207) ✓
- `save_categories` (224) ✓
- `suggested_sources` (266) ✓ (uses bare `_require_user`, return ignored — auth
  enforced via raise on no-session)
- `follow_sources` (330) ✓
- `notifications_enabled` (393) ✓
- `complete_onboarding` (424) ✓
- `sample_signal` (451) ✓
- `sample_feed` (517) ✓
- `goals_state` (529) ✓
- `mark_goal` (583) ✓
- `dismiss_widget` (595) ✓
- `tour_state` (661) ✓
- `tour_complete` (673) ✓
- `tour_skip` (693) ✓
- `admin_metrics_json` (799) ✓ `_require_admin`
- `admin_onboarding_page` (808) ✓ `_require_admin`

**INFO-3** — Three GET endpoints (`/api/onboarding/suggested-sources`,
`/api/onboarding/sample-signal`, `/api/feed/sample`) return JSON that is not
sensitive per se (sample data, public source rankings) but still require auth.
Defence-in-depth is fine; just noting the data isn't itself per-user.

**MED-2** *(restated from Top-3)* — Dev-bypass interaction. `_require_user`
delegates to `server.current_user`, which returns a synthetic dev session for
any localhost request (lines 2238-2256 of `server.py`). If reverse-proxy
config ever leaks `127.0.0.1` as `request.client.host`, every onboarding write
becomes anonymous-writable as the dev user. This is a `server.py` config
issue, not an `onboarding_routes.py` bug, but the surface exists here.

### D. Onboarding-token replay

**No such token exists in this module.** `git grep` finds no
`onboarding_token`, no `magic_link`, no JWT, no nonce issued by any handler
here. Onboarding state is keyed entirely on the authenticated session →
`users.id`. The only token-shaped value near the flow is Stripe's
`{CHECKOUT_SESSION_ID}` query parameter, set in
`gateway/subproduct_signup_routes.py:126` as part of the Stripe success_url
redirect to `/onboarding?subproduct=<slug>&session_id=…`. **`onboarding_page`
(line 169-186) does not read `request.query_params`.** The session_id is
purely a hint to client-side JS for displaying a "thanks for purchasing"
pane via the static template. No server-side validation of the session_id
occurs in this file — that responsibility lives on the Stripe webhook
handler, which is out of scope.

**Implication:** there is no replay surface in this file. An attacker who
replays a stale `?session_id=` URL re-renders the same page; nothing is
persisted from the query string.

**INFO (not a finding):** If a future change adds server-side `session_id`
verification to `onboarding_page` (e.g. "if session_id matches an unclaimed
Stripe checkout, auto-grant subproduct"), the replay surface opens at that
moment. Today it does not exist.

---

## Additional observations (LOW / INFO, not in top 3)

**LOW (unnumbered)** — `_sample_narrative` (lines 483-492) interpolates
`signal.get("content")` directly into a string that is returned in the JSON
`narrative` field. Truncated at 180 chars but no HTML escape, no quote
escape. Returned as JSON so a JSON consumer is safe; if any caller ever
copies the narrative into an HTML context client-side without escaping, the
predictions table content (which is itself ingested from external sources)
could XSS. `signal.content` is sourced from `predictions` / `best_bets`
upstream — vector depends on input sanitisation at extraction time, not in
this file.

**INFO** — Module opens a fresh SQLite connection per request
(`_connect()` at line 85) and closes in `finally`. No pooling. Under load
this could exhaust file descriptors before the global rate limiter
(1771 in server.py, 60 req/min/IP) catches it. Operational, not security.

**INFO** — `_table_exists` (line 92) is called with a string constant in
every site; no user input reaches it. No SQL injection.

**INFO** — `admin_onboarding_page` (line 807) builds HTML via f-strings.
Every interpolated value is either an integer (`metrics.get(..., 0)`) or
passed through `html.escape` (line 820, 838, 871). `metrics.get` values
that are floats (e.g. `completion_pct`) are not escaped but they are typed
as floats so HTML injection is not reachable. Clean.

---

## Recommended fixes (not implemented per task constraints)

1. **MED-1**: gate `complete_onboarding` on a server-side check that
   `user_onboarding.step_completed >= 4` before writing
   `users.onboarding_completed`. Or — better — derive `onboarding_completed`
   in the read layer (`queries/onboarding.py:get_onboarding_state`) from the
   `user_onboarding` row state and stop writing the user column entirely.
2. **MED-2**: tighten `is_local_host` in `server.py` to also require
   `IS_PRODUCTION = false`, so a misconfigured prod proxy can never trigger
   dev-bypass. Tracking note: not a fix for `onboarding_routes.py`.
3. **LOW-1**: cap each entry in `handles` to a sane length (e.g. 64 chars)
   and restrict the character set (`[A-Za-z0-9_.-]+`) before persistence.
4. **LOW-3**: pick one of (dismissed | completed) per row in metrics — the
   `SUM(CASE …)` clauses currently double-count.
5. **LOW-4**: hoist `VALID_CATEGORIES` to a shared module imported by both
   write and read sites.

---

## Verdict

No critical or high findings. The module is generally well-built: every
handler authenticates, parameterized SQL throughout, no command injection,
no SSRF, no SQL injection, no auth-bypass surface in the file as-is. The
two medium findings are latent: MED-1 only becomes exploitable when the
documented future middleware ships; MED-2 is a server.py config interaction
that this file participates in but doesn't cause. Lows are reporting-quality
and minor bloat issues.

Recommended posture: ship MED-1's fix (gate the completion write) **before**
the dashboards redirect middleware lands.
