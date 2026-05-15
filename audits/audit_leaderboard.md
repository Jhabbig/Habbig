# Adversarial audit — leaderboard flow

**Scope.** Audit of the opt-in user leaderboard: opt-in/opt-out semantics,
handle-only display vs email leakage, score-tampering surface, and
anti-bot / rate-limit on read. Files in scope:

- `gateway/routes_referrals.py` (383 lines) — `/leaderboard`,
  `/api/leaderboard`, `/api/leaderboard/participate` (POST + DELETE),
  `/api/leaderboard/me`
- `gateway/db_referrals.py` (506 lines) — `set_leaderboard_participation`,
  `get_leaderboard_opt_in`, `get_leaderboard`,
  `count_leaderboard_participants`, `get_user_leaderboard_rank`,
  `upsert_user_accuracy`
- `gateway/jobs/referral_jobs.py` (408 lines) — only the
  `compute_user_leaderboard_scores` daily scorer (lines 297-407)
- `gateway/static/leaderboard.html` + `gateway/static/leaderboard.js` —
  the public-facing page
- `gateway/migrations/023_referrals_leaderboard.py` — schema
- `gateway/migrations/031_user_predictions.py` — score-source schema
- `gateway/queries/predictions.py` (`create_user_prediction`,
  `update_user_prediction`) — the upstream score-input write paths
- `gateway/db_takes.py` (`user_opts_in_public_takes`) — second consumer
  of the same opt-in flag
- `gateway/take_routes.py` (`/u/{user_id}/takes`) — second consumer

Supporting files consulted to ground each finding:

- `gateway/server.py` (`current_user`, CSRF middleware,
  `GlobalRateLimitMiddleware`, `is_local_host` dev bypass)
- `gateway/middleware/bulk_data_ratelimit.py` — row-budget backstop
- `gateway/security/input_hygiene.py` (`clean_text`)
- `gateway/seo.py` — `/leaderboard` is in `NOINDEX_PATHS`
- `gateway/tests/e2e/test_leaderboard_flow.py` — intended invariants

No code was modified.

---

## Severity counts

| Severity      | Count |
| ------------- | ----- |
| Critical      | 0     |
| High          | 2     |
| Medium        | 4     |
| Low           | 4     |
| Informational | 2     |
| **Total**     | **12** |

---

## Top 3 (must-fix)

1. **H1 — `/leaderboard` and `/api/leaderboard` are gated only by
   "is logged in", not by an active subscription, despite the docstrings
   claiming "Paying subscribers only".** `routes_referrals.py:269-281`
   (page handler) and `:283-326` (API handler) call `_current_user()` and
   redirect to `/token` only when the result is falsy. The matching
   `user_prediction_routes.py:62-80` shows the project's actual
   subscriber gate (`_require_paid_user` → `db.get_user_subscription_tier`
   → 402 on `none`/`free`), and `server.py:7675, 7886, 8584` use
   `db.has_active_subscription(...)` for other paywalled features.
   Neither is invoked here. The impact is two-fold: (a) any free /
   trial / expired-subscription account can read the full opt-in
   leaderboard plus `total_users_approx` (active-subscriber count) and
   `my_rank` data, and (b) the inconsistency means a future change that
   *does* add a paywall will silently break the opt-in privacy contract
   ("only paying subscribers see your handle") that the user agreed to
   when they opted in. Either add an inline `has_active_subscription`
   check or wrap the routes with the same `_require_paid_user` helper
   `user_prediction_routes.py` already uses.

2. **H2 — Leaderboard endpoints have no inline anti-bot / scraping
   rate-limit beyond the 600-req/min global cap; combined with H1 this
   lets any free account scrape the full 500-row leaderboard plus
   `my_rank` + `total_users_approx` repeatedly with low effort.**
   `routes_referrals.py:283-326` has zero rate-limit calls (no
   `db.rate_limit_hit`, no `_is_rate_limited`). The route returns up
   to 500 rows per call (`limit ≤ 500` enforced at `:294`), and the
   bulk-data middleware (`middleware/bulk_data_ratelimit.py:42`) caps
   at 5,000 rows/hour for authenticated users — so up to 10
   full-leaderboard pulls per hour per account. Compare to
   `/api/invite/{code}/accept` in the same file (`:123, :125`) which
   layers per-IP (20/h) and per-email (3/day) on top of the global
   cap. Add an inline per-user / per-IP limit at the
   `/api/leaderboard` GET (e.g. 60/hour/user) so a scraper account
   can't enumerate the participant set faster than the underlying
   nightly recompute. The `participate` POST and `participate` DELETE
   also lack inline limits — a single account can churn its
   `leaderboard_handle` (and burn through the UNIQUE-index races) at
   600 req/min without tripping anything.

3. **M1 — `total_users_approx` discloses an exact count of active
   subscribers to every logged-in user (including, given H1, free
   ones).** `routes_referrals.py:301-305` runs
   `SELECT COUNT(*) FROM users WHERE COALESCE(is_deleted,0)=0 AND
   COALESCE(suspended,0)=0`, returned at `:324` as
   `total_users_approx`. The field name implies fuzziness but the
   query is exact. This is a footer-line "X of Y are on the
   leaderboard" feature (`leaderboard.js:39-43`) — the privacy cost
   (exposing a real-time count of paying subscribers + free signups
   to anyone with a session) outweighs the UX gain. Either bucket the
   value (round to nearest 50/100) or restrict the footer message to
   participants only.

---

## Findings

### H — High

**H1 — Leaderboard routes are not paywalled despite docstrings.**
`routes_referrals.py:269` says "Paying subscribers only." `:287` says
"Paid-only — same guard as the page." Neither is true: both handlers
only check `_current_user(request)` for truthiness. A user on
`subscription_tier='free'` (or an expired-subscription user whose
session has not yet logged out) passes the gate. See "Top 3" #1 above
for full context and fix sketch.

**H2 — No inline rate-limit on read or opt-in mutations.** See "Top 3"
#2. In addition to the read-scraping concern, the
`POST /api/leaderboard/participate` endpoint has no inline rate-limit,
so the `set_leaderboard_participation` UNIQUE-index path
(`db_referrals.py:339-348`) can be hammered to brute-force the handle
namespace. The error message at `:347` is "That display name is taken"
— a precise oracle. Combined with no inline limit, this is a handle
enumeration sink (e.g. confirming whether a known handle exists on the
service). Layer a `db.rate_limit_hit(f"lb_participate:{user_id}",
limit=10, window=3600)` on the POST so the oracle is bounded.

### M — Medium

**M1 — `total_users_approx` discloses exact active-subscriber count.**
See Top 3 #3 above.

**M2 — `user_accuracy` rows are not purged on opt-out, so re-opting in
restores the prior rank instantly with no re-aggregation.**
`db_referrals.set_leaderboard_participation` (`db_referrals.py:325-331`)
sets `leaderboard_participation=0` on opt-out but does NOT delete the
matching `user_accuracy` row. The nightly scorer
(`jobs/referral_jobs.py:329-335`) only re-scores opted-in users, so an
opted-out user's score is frozen, not removed. If the user opts back
in any time before the next 03:00 UTC cron, they appear with whatever
accuracy they had at opt-out. Two implications:
- Privacy: "I clicked opt-out" does not mean "the system forgot my
  rank"; it means "I'm hidden until I click opt-in again". A user
  expecting GDPR-style deletion gets stale-cache surprise instead.
- Manipulation: a user can lock in a peak rank by opting out the moment
  a streak hits, then opt back in months later without their stale
  rank getting recomputed against newer markets. The scorer is a full
  recompute when invoked but only over the *currently-opted-in* set,
  so the stale row sits untouched in `user_accuracy` indefinitely.
Either purge the row on opt-out (`DELETE FROM user_accuracy WHERE
user_id = ?` inside the same transaction) or include opted-out rows
in the nightly recompute.

**M3 — Handle uniqueness is BINARY-collated, so `alice` and `Alice`
both register and render as distinct entries — a visual-impersonation
sink.** `db_referrals.py:339-348` does
`UPDATE users SET leaderboard_handle = ? WHERE id = ?` and relies on
the UNIQUE partial index from `migrations/023:67-69`. SQLite indexes
default to BINARY collation; the index does *not* specify
`COLLATE NOCASE`. Combined with no case-fold at write time
(`db_referrals.py:333` strips whitespace but preserves case per the
docstring "Stored as-submitted"), two users can each claim
`Anders` and `anders`, and the leaderboard table renders both at
`@Anders` and `@anders`. Switch the index to `COLLATE NOCASE` and
lowercase the handle at write — or at minimum case-fold the comparison
without touching the stored form.

**M4 — `/u/{user_id}/takes` reuses `leaderboard_participation` as
public-takes consent without a separate opt-in.** `db_takes.py:210-233`
and `take_routes.py:659-712` use the leaderboard flag as the
"public profile" gate. This is documented as intentional (`db_takes.py:213`)
but it means the same checkbox at `/settings#privacy` controls *two*
distinct surfaces: the ranking-by-accuracy leaderboard and a
public-best-takes profile with `reasoning` text (up to 280 chars,
`take_routes.py:737`). Users who opt in expecting "name on the
leaderboard" also expose `/u/{user_id}/takes` with their take
reasoning. The opt-in copy (not audited here) needs to disclose this
joint scope, or split into two flags — repurposing a privacy flag
without UX disclosure is a soft-fail of informed consent.

### L — Low

**L1 — Opt-out helper does not surface the IntegrityError race on
display-name UPDATE.** `db_referrals.py:339-348` wraps the UPDATE in a
try/except IntegrityError and returns
`{"ok": False, "error": "That display name is taken."}`. Only
IntegrityError is caught; other `sqlite3` errors propagate and bubble
into FastAPI's default 500. The API route
(`routes_referrals.py:352-362`) maps `"taken"`-containing errors to
409 and everything else to 400 — a 500 here would route past the
mapper. Catch `sqlite3.OperationalError` (db-locked retry surface) and
return a 503 with `Retry-After` so the client can back off.

**L2 — Test in `tests/e2e/test_leaderboard_flow.py:46-52` POSTs to
`/api/leaderboard/opt-out`, an endpoint that does not exist.** The real
opt-out route is `DELETE /api/leaderboard/participate`
(`routes_referrals.py:365-372`). The test asserts `r.status_code < 400`,
so any 404 from the catch-all OR a 200 from a future stub would both
pass/fail in the wrong direction. Either fix the test or stand up an
alias route. Not a runtime security finding, but it means there is no
e2e coverage that opt-out actually flips the flag.

**L3 — `leaderboard.js:28` and `:57` use a manual `esc()` for the
`@handle` and rank values rather than `textContent`.** The escape
function is correct for the five HTML special characters, and the
write path (`db_referrals.py:334` regex `[A-Za-z0-9_-]{3,24}`) confines
handles to safe ASCII. So no XSS in the current shape. But this is a
defence-in-depth tax: a future widening of the handle alphabet (e.g.
to allow `.` or `'`) would silently expose the renderer. Switch to
`textContent` for `handle` / `total_predictions` / `correct_predictions`
and reserve `innerHTML` for the surrounding template only.

**L4 — `set_leaderboard_participation` does not normalise handles via
`clean_text`, only the calling route does.** `routes_referrals.py:343-347`
calls `clean_text(body["display_name"], max_len=40, required=True)`
*before* invoking the db helper, so production traffic is clean. But
`db_referrals.py:312-348` is a public API of the module: a future
caller from a CLI/admin path that constructs a handle differently
would skip the NFC-normalise / zero-width-strip / control-char-reject
pipeline. Move the `clean_text` call (or its essential rules) inside
`set_leaderboard_participation` so the db helper is safe by default.

### I — Informational

**I1 — `compute_user_leaderboard_scores` operates on
`user_predictions.resolved = 1`, but no production code path flips
that flag.** `grep -rn "UPDATE user_predictions" --include='*.py' |
grep -v tests/` in the gateway returns only `queries/predictions.py:437`
— which is `update_user_prediction`, gated to `WHERE resolved = 0`
(`:438`). There is no equivalent `resolve_user_predictions_for_market`
helper analogous to `resolve_predictions_for_market` at `:80-100`. The
nightly scorer (`jobs/referral_jobs.py:349-354`) thus reads zero rows
in production, and the leaderboard renders only the "Unranked"
participants — the SHIP/no-data state the table-empty branch in
`leaderboard.js:15-22` describes. This is not a security flaw per se,
but it means the entire surface audited here is effectively *latent*:
no real user score can be tampered with because no real user score
exists yet. The downstream impact is that once the resolver lands,
none of the H/M findings above have been hardened against actual data
— pre-launch is the cheap time to fix them.

**I2 — `get_user_leaderboard_rank` returns rank to opted-OUT users as
long as they have a `user_accuracy` row.** `db_referrals.py:475-505`
filters the comparison cohort to opted-in users
(`u.leaderboard_participation = 1` at `:498`) but does NOT check the
caller's own opt-in state — `:488` reads
`SELECT accuracy_all_time FROM user_accuracy WHERE user_id = ?` with
no participation gate. This is the symptom of M2: an opted-out user
keeps querying `/api/leaderboard/me` and seeing their stale rank
against the live cohort. Not actionable as a vulnerability — the user
already has the data — but the API contract is "your position on the
leaderboard you are not on", which is conceptually off.

---

## Out of scope

- `/api/insider/leaderboard` (`insider_routes.py:159-180`) — a separate
  trader-leaderboard surface keyed on a different table; not the
  user-accuracy leaderboard.
- Source-credibility leaderboard rendered via `/sources` listings;
  separate pipeline, not opt-in.
- Newsletter `referred_by` waitlist mechanics; unrelated table.
- The referral reward flow itself — covered by `audit_referrals.md` in
  this same folder.

## Verification notes

- Opt-in respected at query time: `db_referrals.get_leaderboard`
  (`:417-460`) and `count_leaderboard_participants` (`:463-472`) both
  filter `leaderboard_participation = 1`. A user with the flag at 0
  cannot appear in either list. **Pass.**
- Email leak: the `/api/leaderboard` row builder
  (`routes_referrals.py:307-318`) selects only `leaderboard_handle`,
  `total_predictions`, `correct_predictions`, `accuracy` — no email,
  no real-name fields, no `users.username`. The fallback display is
  `f"user_{r['user_id']}"` (`:309`), which leaks the numeric user id
  but not email/handle. **Pass with caveat** — the numeric id is
  joinable against any other endpoint that exposes a `user_id`
  field (e.g. `/u/{user_id}/takes`), so a participant who never
  opted into the takes profile *can* still be linked across
  surfaces by id. Recommend hashing the fallback to a per-handle
  pseudonym.
- Score-tampering at the write surface: `create_user_prediction`
  (`queries/predictions.py:351-389`) inserts with `resolved=0` and
  `resolved_correct=NULL` (column defaults); `update_user_prediction`
  (`:410-441`) only allows updating `predicted_probability`,
  `reasoning`, `is_public` and only `WHERE resolved = 0`. There is no
  route or DB helper that lets a non-admin write `resolved=1` or
  `resolved_correct=1`. **Pass.** (See I1 for the corollary that no
  *legitimate* path writes those either.)
- CSRF on the mutating endpoints: `CSRFMiddleware`
  (`server.py:1280-1394`) protects POST unconditionally and
  PATCH/PUT/DELETE under the `CSRF_PATCH_DELETE_ENFORCE` flag. The
  opt-in POST and opt-out DELETE both flow through it; no explicit
  exemption in `_CSRF_EXEMPT_POSTS`. **Pass** (assuming the rollout
  flag is on for DELETE in production).
