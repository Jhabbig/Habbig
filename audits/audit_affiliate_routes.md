# Adversarial audit — `gateway/affiliate_routes.py`

**Scope.** Adversarial review of `gateway/affiliate_routes.py` (710 lines)
with focus areas requested by the user:

1. Referral-link forgery / self-referral
2. Conversion attribution races
3. Payout calculation correctness
4. Admin guard on payout actions
5. IDOR on viewing other affiliates' stats
6. Link-creation rate-limit

Supporting files consulted to ground each finding:

- `gateway/db_affiliate.py` (DB helpers — schema-touching logic)
- `gateway/migrations/033_affiliate_program.py` (schema, indices)
- `gateway/jobs/affiliate_jobs.py` (commission calc cron)
- `gateway/server.py` (`_require_admin_user`, `_is_rate_limited`,
  `_get_client_ip`, CSRF middleware, `CSRF_PATCH_DELETE_ENFORCE` flag)
- `gateway/server_features.py` (the `/auth/register` handler)
- `gateway/tests/test_affiliate.py` (intended invariants)

No code was modified.

---

## Severity counts

| Severity | Count |
| --- | --- |
| Critical | 0 |
| High     | 3 |
| Medium   | 4 |
| Low      | 5 |
| Informational | 3 |
| **Total** | **15** |

---

## Top 3 (must-fix)

1. **H1 — `maybe_attribute_signup` is never wired into `/auth/register`,
   yet rate-limit / cookie / dashboard machinery acts as if it is.**
   The hook is defined at `affiliate_routes.py:91` and documented as
   "exported for `/auth/register` to call inline after `create_user`
   succeeds" (module docstring, lines 21-24). The actual register
   handler at `gateway/server_features.py:1536-1693` does **not** call
   it — only the share-loop attribution at lines 1647-1690 runs. Result:
   every click is recorded, the cookie is set for 90 days, the affiliate
   dashboard renders, the admin can mark conversions paid — but no
   conversion will ever be attributed by a real registration. Hidden
   regression because the unit test calls `da.attach_signup_to_affiliate`
   directly (`tests/test_affiliate.py:222-267`) and never exercises the
   HTTP register path. Severity: **High** — the feature appears working
   but mis-attributes every real signup as "not attributed" (silent data
   loss); compounded by the fact that anyone who has built process
   around the unwired pipeline (e.g. admin payouts) is acting on phantom
   data.

2. **H2 — Step-3 of `attach_signup_to_affiliate` accepts a signup with
   no recorded click and no integrity check on the cookie value.**
   `db_affiliate.py:387-402` inserts a fresh conversion row with
   `source_note = "cookie_without_click"` whenever the cookie is
   present but no unclaimed click row exists in the 90-day window.
   Because the `affiliate_code` cookie is set with `httponly=True` but
   **unsigned** (`affiliate_routes.py:73-81`), an attacker / collaborator
   can hand-set it via the browser's dev tools or a malicious script
   pre-loaded on a partner site, walk to `/auth/register`, and have
   themselves attributed to the affiliate of their choice without ever
   visiting `/partner/{code}`. Combined with the lack of self-referral
   guard (see H3), an affiliate can self-attribute by setting the
   cookie value to their own `affiliate_code` in their browser before
   creating a second account (or asking a buddy to sign up on the same
   machine after they paste the value in). The 90-day attribution
   window means a single cookie-set holds for a full quarter.
   *Note: only exploitable once H1 is fixed and the hook actually
   runs — but if you fix H1 alone without fixing this, you ship a
   forgeable attribution channel on day one.*

3. **H3 — No self-referral / self-conversion check anywhere in the
   attribution path.**
   `attach_signup_to_affiliate` (`db_affiliate.py:335-422`) never
   compares `affiliate_account_id`'s owning `user_id` to the freshly
   registered `user_id`. The "re-attribution guard" at lines 360-366
   only blocks a user from being claimed *twice*, not from being claimed
   by their own affiliate account on first signup. The product spec
   makes affiliate accounts admin-created and bound 1:1 to an existing
   user, so a literal self-credit by the same account requires an admin
   to first create an AffiliateAccount and the same person to then
   register a second account — possible because invite tokens are
   issued in bulk by admin and emails are not deduplicated across
   tokens. A more realistic path is the **collusion variant**: affiliate
   Alice hands her tracking link to friend Bob, Bob signs up with a
   Stripe-funded plan using Alice's referral cookie, and Alice earns
   commission on a signup she effectively bought herself. Because
   commission calc multiplies `first_payment × commission_rate` with no
   fraud heuristics and "the admin sends out the actual payment
   out-of-band" (route docstring, lines 690-693), nothing detects this.
   Self-referral should reject when `aff.user_id == referred_user_id`;
   collusion mitigation requires at minimum an IP / fingerprint match
   check between the click-row and the registering user (the
   `click_fingerprint` column exists at `migrations/033_*.py:113` and
   is being populated, but is never read by the attribution path).

---

## Full findings

### High

#### H1 — Attribution hook unwired (described above)

- **Files:** `gateway/affiliate_routes.py:91-120`,
  `gateway/server_features.py:1536-1693`.
- **Fix:** call `maybe_attribute_signup(request, user_id)` after the
  `db.create_user` line at `server_features.py:1604` (before the
  response is built so the cookie can be cleared on the same response).
  Add an integration test that POSTs `/auth/register` with the
  `affiliate_code` cookie set and asserts an `affiliate_conversions`
  row materialises.
- **Impact:** silently zero attribution.

#### H2 — Unsigned attribution cookie + Step-3 fall-through

- **Files:** `gateway/affiliate_routes.py:65-81`,
  `gateway/db_affiliate.py:387-402`.
- **Why the cookie is forgeable:** `_set_affiliate_cookie` does not
  HMAC-sign the value. `_read_affiliate_cookie` reads the raw value
  and feeds it to `get_affiliate_by_code` which only checks `is_active`
  — there is no proof the cookie was issued by `/partner/{code}`.
- **Why Step-3 amplifies the risk:** `attach_signup_to_affiliate`
  treats "cookie present, no matching click" as a valid attribution
  state. The intent (per docstring) is to survive a DB wipe, but the
  side effect is that a hand-crafted cookie always succeeds.
- **Fix options (any one of these is enough):**
  - Drop Step-3 entirely. Only attribute when a real click row is
    available to claim. (Cheapest; loses attribution for users who
    clear DB but keep the cookie — acceptable.)
  - Sign the cookie with `affiliate_code|HMAC(secret, code|expiry)`
    and validate before honouring it.
  - Require a recent server-side click record by IP / fingerprint
    pair before honouring a Step-3 cookie.

#### H3 — No self-referral guard (described above)

- **Files:** `gateway/db_affiliate.py:335-422`.
- **Fix:** at the top of `attach_signup_to_affiliate`, query
  `affiliate_accounts.user_id` for `affiliate_account_id`; if it equals
  the incoming `user_id`, return `None` and log a warning. Additionally
  compare `click_fingerprint` (already captured) against the registering
  user's `_get_client_ip` for the collusion-detection signal — flag for
  admin review rather than blocking outright.

---

### Medium

#### M1 — `PATCH /admin/affiliates/{id}` is soft-CSRF during rollout

- **Files:** `gateway/affiliate_routes.py:640-685`, `gateway/server.py:1197-1215`.
- The route is gated by `_require_admin_user` and an explicit
  `admin_level >= 2` check (`affiliate_routes.py:647`), but the CSRF
  middleware only enforces CSRF on POST today (rollout flag
  `CSRF_PATCH_DELETE_ENFORCE` defaults `false`). If a super-admin has
  an XSS-able session on the same browser context, an attacker can
  trigger commission-rate bumps via a forged PATCH. **Action:** confirm
  flag is `true` in production env; the codebase ships the flag off by
  default which is the gap to track.

#### M2 — `mark_affiliate_payout_complete` is **not** restricted to super-admin

- **Files:** `gateway/affiliate_routes.py:688-710`.
- `admin_affiliates_update` (PATCH) gates commission-rate / tier / is_active
  edits behind `admin_level >= 2` (H8 comment at lines 643-647), but the
  `POST /admin/affiliates/{id}/payout` handler that flips every unpaid
  commission to `commission_paid=1` only checks `_require_admin_user`
  (level 1). A level-1 admin therefore cannot raise an affiliate's rate
  but **can** mark a fabricated set of conversions paid for any
  affiliate they want, which:
  - Removes those rows from the `pending_payouts` queue (admin loses
    visibility that the money is owed).
  - Sets `commission_paid_by_admin_id` to themselves — audit trail
    points at them, but they triggered it.
  - Decouples the DB "paid" state from the real out-of-band payment
    — depending on ops workflow, the next admin who actually wires the
    money does so against a zeroed pending balance, or worse pays twice.
  **Fix:** apply the same `admin_level >= 2` gate as the PATCH handler.
  Add a confirmation step (e.g. include the expected `total_pence` in
  the request body and reject if it doesn't match the server's tally).

#### M3 — `total_earnings_pence` denormalised counter can desync

- **Files:** `gateway/db_affiliate.py:483-499`, `migrations/033_*.py:50`.
- `record_commission_calculated` increments
  `affiliate_accounts.total_earnings_pence` in a separate UPDATE from
  the per-row stamp. Both run inside the same `with db.conn()` block
  so SQLite gives them transactional atomicity, but the field is also
  surfaced to admin (`list_affiliates` joins this), shown to affiliates
  (`sum_affiliate_commissions` re-aggregates and ignores it — so they
  see fresh totals), and used for sorting. If a refund / clawback path
  is ever added (none exists today), `total_earnings_pence` will fall
  out of sync because there's no decrement path. **Fix:** drop the
  cached column and read from `sum_affiliate_commissions` everywhere,
  or add a `recompute_total_earnings` admin button as a stop-gap.

#### M4 — Attribution race: two simultaneous registrations with the same cookie can both succeed

- **Files:** `gateway/db_affiliate.py:359-422`.
- The "Step 1 / Step 2 / Step 3" branch reads `affiliate_conversions`
  with `referred_user_id = ?` then writes a row, all inside one
  `with db.conn() as c:` block. SQLite's WAL mode serialises writers
  so the inner UPDATE in Step 2 is safe against a duplicate row for
  the *same* `referred_user_id`. But two distinct registrations
  (different new users) clicking the same affiliate's link within
  seconds will both call Step 2's "claim the most recent unclaimed
  click" query, both fetch the same row id, and the second UPDATE
  will silently overwrite the first's `referred_user_id`. SQLite's
  isolation does not catch this because the SELECT is not paired with
  a `FOR UPDATE` / serializable transaction. **Reproducer:** two
  parallel requests hitting `/auth/register` with the same affiliate
  cookie. **Fix:** make Step 2 a single `UPDATE … WHERE id = (SELECT
  id FROM … LIMIT 1) AND referred_user_id IS NULL` and check
  `rowcount == 1`; on miss, fall through to Step 3.

---

### Low

#### L1 — `POST /api/v1/affiliate/links` has no per-account rate limit

- **Files:** `gateway/affiliate_routes.py:364-400`,
  `gateway/db_affiliate.py:231-258`.
- The only protection is the global per-IP middleware (server.py
  lines 1731+). Because `create_affiliate_link` is idempotent on
  `(affiliate_account_id, utm_campaign)`, abuse is bounded to ~40-char
  slug space, but an authenticated affiliate can still script several
  hundred unique slugs to bloat `affiliate_links` and clutter their
  own dashboard. **Fix:** add `_is_rate_limited(f"affiliate_link_create:
  {aff['id']}", limit=20, window=3600)` and a soft cap (e.g. 100 links
  per account) in `create_affiliate_link`.

#### L2 — `_get_client_ip` value stored verbatim as `click_fingerprint`

- **Files:** `gateway/affiliate_routes.py:134`, `gateway/affiliate_routes.py:106`,
  `gateway/db_affiliate.py:288-316`.
- `click_fingerprint` is logged plaintext (the user's IP). If the
  audit log / DB dump ever leaks, this is a GDPR record. The column
  is also used as the `fallback_fingerprint` for Step-3 attribution.
  **Fix:** hash with a server-side secret (`hmac.new(secret,
  ip.encode(), 'sha256').hexdigest()`) before persisting; preserves
  matchability for fraud heuristics while neutralising the privacy
  exposure.

#### L3 — `anonymise_email` leaks the local part fully

- **Files:** `gateway/db_affiliate.py:633-646`.
- `jake@example.com → jake@.com` keeps the full local part. For
  unusual local parts (`firstname.lastname.dob1985`) this is a
  near-unique identifier. The function is used both in the affiliate
  dashboard (`api_affiliate_conversions` at lines 403-425) and in the
  HTML render. **Fix:** truncate local part to first two chars +
  `***`, e.g. `ja***@.com`.

#### L4 — `affiliate_payout_req` rate limit returns 200 OK with a misleading "already requested" message

- **Files:** `gateway/affiliate_routes.py:449-466`.
- On hit, the response body says "Already requested; admin will
  process shortly." But the actual admin email is only sent on the
  *first* request inside the window — every subsequent call inside
  the hour gets the friendly 200 and **no email is queued**. An
  affiliate hitting the button during a transient
  email-system-down window will believe their request reached an admin
  and won't retry. **Fix:** return 429 with a clear "try again in N
  minutes" instead, or actually re-enqueue the email on each call
  (with idempotency key).

#### L5 — `_send_admin_payout_notification` `asyncio.get_running_loop()` fallback to `asyncio.run` is a footgun

- **Files:** `gateway/affiliate_routes.py:519-525`.
- The `except RuntimeError: asyncio.run(_go())` branch would block
  the handler for the entire email round-trip if it ever fires. The
  comment says "shouldn't happen — route handler always has a loop"
  which is true today, but a future change to call this from a
  sync test path will deadlock. Cheap fix: drop the `RuntimeError`
  branch entirely and `log.exception` instead.

---

### Informational

#### I1 — `/p/{code}` and `/partner/{code}` redirect silently on bad code, but `if not affiliate or not affiliate["is_active"]` happens **after** the rate-limit check

- **Files:** `gateway/affiliate_routes.py:126-168`.
- A burst of `/p/INVALID-{n}` requests still consumes the per-IP
  budget — fine, by design. Worth noting because the rate-limit message
  "Generous limit; not a DDoS defense" (line 137) is correct but the
  redirect-on-limit-hit (302 to `/` instead of 429) hides the throttle
  from any honest debugging the user is doing.

#### I2 — Admin list (`GET /admin/affiliates`) returns `affiliate_code` for every account in the HTML

- **Files:** `gateway/affiliate_routes.py:531-585`.
- Admin-only, so this is fine, but worth noting that the list
  rendering puts every code in plaintext. If the admin page is ever
  cached upstream (CDN edge cache), the codes leak. The `/admin/`
  routes have CSP and `_require_admin_user`, so the actual risk is
  configuration drift.

#### I3 — `migrations/033_affiliate_program.py` declares
  `affiliate_link_id REFERENCES affiliate_links(id) ON DELETE SET NULL`
  but no foreign-key enforcement pragma is enabled at conn() level

- **Files:** `gateway/migrations/033_affiliate_program.py:108-110`.
- Standard for the SQLite codebase, but the ON DELETE SET NULL clause
  is a no-op unless `PRAGMA foreign_keys=ON` is set on every connection.
  If a link is deleted out-of-band (none of the affiliate code paths
  do this today), the conversion row's `affiliate_link_id` will dangle.

---

## Coverage notes (what I did NOT cover)

- Stripe webhook → `mark_affiliate_conversion_paid` linkage (the
  Stripe handler that calls this is "not yet wired" per
  `db_affiliate.py:430` and `jobs/affiliate_jobs.py:14-18`; no review
  of an unwritten module).
- The HTML template `settings_affiliate.html` itself (only the
  context-building Python is in scope).
- The `email_system.service.EmailService` send path called by
  `_send_admin_payout_notification` — outside affiliate_routes.

## Verification footnote

Every line and file reference in this audit was opened against the
current `feature/platform-build` branch tip (commit `e98cec6`). No
attempt was made to exploit any of the findings on a live system.
