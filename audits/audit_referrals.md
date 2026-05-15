# Adversarial audit — referrals routes

**Scope.** Audit of the private referral program: invite-link landing,
acceptance, code uniqueness, conversion attribution, reward-grant
correctness, and payout abuse. Files in scope:

- `gateway/routes_referrals.py` (382 lines) — public + authenticated routes
- `gateway/public_routes.py` (only the `referred_by` newsletter waitlist
  mechanic; see Out-of-Scope)
- `gateway/admin_test_emails_routes.py` (only test-email triggers for
  referral templates; non-production data path)

Supporting files consulted to ground each finding:

- `gateway/db_referrals.py` — referral-row + leaderboard DB layer
- `gateway/backend/referrals.py` — pure reward-tiering logic
- `gateway/jobs/referral_jobs.py` — daily reward-grant + leaderboard cron
- `gateway/queries/subscriptions.py` — the `upsert_subscription` →
  `mark_referral_converted` conversion hook
- `gateway/queries/auth.py` — `create_invite_token` / `claim_invite_token`
- `gateway/server_features.py` (`/auth/register` handler, lines 1536-1693)
- `gateway/migrations/023_referrals_leaderboard.py` — schema
- `gateway/tests/test_referrals.py` — intended invariants

No code was modified.

The two `grep` hits in `public_routes.py` and `admin_test_emails_routes.py`
refer to (a) the **newsletter** pre-release waitlist's separate
`referral_code` column on `newsletter_subscribers` (a totally different
table and feature), and (b) admin test-email plumbing for the
`referral_invite` / `referral_reward` templates. Neither affects the
production reward pipeline. The real production routes for the
"refer a friend → free month" program all live in `routes_referrals.py`
even though it doesn't follow the `*_routes.py` naming convention; the
greppable spelling difference between `routes_referrals.py` and
`*_routes.py` is itself a finding (see L4).

---

## Severity counts

| Severity      | Count |
| ------------- | ----- |
| Critical      | 1     |
| High          | 3     |
| Medium        | 4     |
| Low           | 4     |
| Informational | 2     |
| **Total**     | **14** |

---

## Top 3 (must-fix)

1. **C1 — `attach_user_to_referral` is never wired into `/auth/register`,
   so every pending `/invite/{code}/accept` referral row stays orphaned
   and is *never* rewarded.** `routes_referrals.py:159-163`
   inserts a `referrals` row with `referred_email` set and
   `referred_user_id` left **NULL** (`db_referrals.create_referral` only
   takes `referred_email` here; the second branch with `referred_user_id`
   isn't reachable from this flow). The invitee later registers via
   `/auth/register` (`server_features.py:1536`); the only post-registration
   referral wiring there is the **share-loop** bridge at lines 1647-1690,
   which creates a *fresh* `referrals` row from a `narve_share_attribution`
   cookie. There is no equivalent code that finds the pending row by
   `referred_email == email` (or by `invite_token_id`) and stamps
   `referred_user_id`. Since `mark_referral_converted`
   (`db_referrals.py:136`) keys exclusively on `referred_user_id`, the
   eventual paid subscription will *never* flip these rows to
   `converted_to_paid = 1`, and `process_referral_rewards`
   (`jobs/referral_jobs.py:66`) will never see them as pending.
   `grep -rn "attach_user_to_referral"` confirms it has **zero production
   call sites** — only the test file calls it.
   Severity: **Critical.** The entire `/invite/{code}` → email → register
   funnel — the canonical referral path advertised on
   `/settings/referrals` — produces zero rewards. The referrer's UI shows
   the invitee stuck at status `"Invited"` forever (`routes_referrals.py:236-237`).
   Only the *secondary* share-loop path actually pays out.

2. **H1 — No self-referral guard on `/api/invite/{code}/accept`; a user
   can invite their own future second email and earn rewards from
   themselves.** `routes_referrals.py:102-188`. The accept endpoint
   validates that the code resolves to a real referrer and that the
   target email doesn't already have an account (`existing` check at
   line 139). But nothing blocks **the referrer themselves** from
   submitting an alias of their own (e.g. their `+1@gmail.com` or a
   throwaway address) and later registering with that email. Because the
   share-loop attribution path in `server_features.py:1667` has an
   explicit `sharer_id != user_id` guard, the absence of the same guard
   here is clearly an oversight rather than a deliberate omission. Once
   C1 is fixed (so `/invite/...` rewards actually fire), the cheapest
   abuse pattern in the codebase is: (a) generate your own referral
   code, (b) accept it with `alias@gmail.com`, (c) register, (d)
   subscribe with a chargeable card or a promo (Stripe trial?), (e)
   collect "1 month free" / "tier upgrade" / "3 months Pro free" at
   counts 1/5/10. Severity: **High** — direct revenue impact and the
   only "abuse vector" the brief explicitly flagged.

3. **H2 — `claim_invite_token` does not enforce that the registering
   email equals the invite's `target_email`.**
   `queries/auth.py:341-357`. The invite-accept flow at
   `routes_referrals.py:152-155` creates the token with
   `target_email=email`. But `/auth/register` only requires that the
   pending-token cookie matches *some* unclaimed token; it never compares
   the supplied email against `target_email`. Combined with the absence
   of self-referral checks (H1), this lets an attacker who intercepts the
   referral email (or who is the inviter themselves) register with a
   completely different identity than the one the inviter typed.
   Concretely: A invites `victim@b.com`, the email arrives at `victim@b.com`,
   but anyone holding the link can register as `attacker@c.com`. The
   referrer believes they referred victim and is credited for converting
   attacker. Severity: **High** — undermines the entire "referral
   identity" model and enables (1) targeted abuse where A and B are the
   same person at different addresses and (2) a quieter abuse where a
   referrer uses a single throwaway invite link to enroll a chain of
   their own emails.

---

## Findings (full)

### Critical

**C1. Orphaned referral rows from the `/invite/{code}` flow are never
attributed and never rewarded** — see Top 3 #1.

Recommendation: in `/auth/register` (after `claim_invite_token`
succeeds, around `server_features.py:1609`), look up the
`invite_token_id`-keyed pending referral row (or fall back to
`referred_email = email`) and call
`db_referrals.attach_user_to_referral(referral_id, user_id)`. Without
this, the entire feature only rewards the share-loop bridge path.

### High

**H1. No self-referral guard** — see Top 3 #2.

Recommendation: in `api_invite_accept`, after resolving `referrer`,
reject when `referrer["email"] == email` or any normalised alias
(e.g. plus-addressing dropped, dots normalised for gmail) matches the
referrer's own account email. Mirror the `sharer_id != user_id` check
at `server_features.py:1667`.

**H2. `claim_invite_token` ignores `target_email`** — see Top 3 #3.

Recommendation: when an invite_token has a non-NULL `target_email`,
require the registering email to match (case-insensitive, normalised).
Alternative: bind the pending referral to the token id so even an
"identity-swap" attacker can't claim a reward they weren't supposed to.

**H3. No per-(referrer, email) uniqueness on the `referrals` table.**
`migrations/023_referrals_leaderboard.py:72-106` defines indexes on
`referrer_user_id`, `referred_user_id`, and `(converted_to_paid,
reward_granted)` but **no UNIQUE constraint** on
`(referrer_user_id, lower(referred_email))` nor on
`(referrer_user_id, referred_user_id)`. Combined with the per-email
rate limit being only 3/day (`routes_referrals.py:125`), an inviter can
spam the same target every 24h and create N identical pending rows.
Once attribution is fixed (C1) and that target registers, the
attach/convert step would only flip one row (the first one matched),
but every duplicate stays around as a *pending* row visible in the
referrer's UI and counted in `stats["total_sent"]`
(`db_referrals.py:245`). A motivated abuser can also use this to
inflate the per-user "Invited" count for social-proof reasons. Severity:
**High** — direct double-grant risk if any future code change adopts
`referred_email` as a join key (e.g. an admin "mark-converted" tool),
and an immediate UI-pollution vector today.

Recommendation: add
`CREATE UNIQUE INDEX idx_referrals_unique_per_referrer
 ON referrals(referrer_user_id, lower(referred_email))
 WHERE referred_email IS NOT NULL`
and treat `IntegrityError` in `create_referral` as a 200-OK idempotent
response.

### Medium

**M1. Reward race: the "stamp first, then revoke gift" pattern leaves
a real money window.** `jobs/referral_jobs.py:177-233`. The job
inserts the `gifted_subscriptions` row at line 182, *then* tries the
atomic stamp at line 207. If two job runs ever overlap (the design
deliberately avoids this by being daily, but `register_job` allows
manual triggering via `/admin/jobs`; see `admin_jobs_routes.py` —
double-clicking the admin button would do it), worker B writes the
gift, then loses the stamp race, then revokes its own gift. Worker A's
gift is fine. But the order of operations means a *real* unrevoked
`gifted_subscriptions` row exists for several DB calls' duration — if
the revoke step in the loser path itself fails (the bare `except` at
line 231 logs and continues without escalating), the user keeps a
duplicate gift indefinitely. There's no audit table for "orphan gift
revocation" to detect this after the fact. Severity: **Medium** — only
fires under operator error (concurrent manual runs), and the bug is
self-mitigating most of the time, but every failure mode here costs
real subscription dollars and there's no monitoring.

Recommendation: invert the order — stamp the referral row first, then
insert the gift only if the stamp succeeded. This makes the worst-case
"abandoned" object a stamp-without-gift (a logged error the operator
can replay) rather than a gift-without-stamp (money handed out twice).

**M2. `mark_referral_converted` is keyed only on `referred_user_id`,
so a single new paying user flips referrals for **every** referrer
they were ever pending under.** `db_referrals.py:136-146`. If user X
was invited by A and **also** independently by B (two separate
`referrals` rows, both with `referred_user_id = X`), the moment X pays
the UPDATE flips both rows, and the daily job will gift **both A and
B** a reward. The brief says "no double-grant per referee", and the
implementation gets it right per *row* but not per *user* — the
correct semantic is "the referee can only ever convert for one
referrer, the first one (oldest pending row)." Severity: **Medium** —
small-N abuse, but exploitable: an attacker who controls two
referrer accounts (e.g. via two free trials) can each invite a single
shared third email and double-collect. Also fires accidentally when
the share-loop bridge (`server_features.py:1670`) creates a referral
*on top of* an existing `/invite/{code}` pending row for the same
email.

Recommendation: change `mark_referral_converted` to flip only the
oldest pending row per `referred_user_id`, e.g. `UPDATE referrals SET …
WHERE id = (SELECT id FROM referrals WHERE referred_user_id = ? AND
converted_to_paid = 0 ORDER BY id ASC LIMIT 1)`.

**M3. `count_converted_referrals` and the reward "conversion number"
counter are computed at job time from a global `COUNT(*)` rather than
from a deterministic ordinal stored on the row.**
`jobs/referral_jobs.py:106-118, 130-132, 154, 174`. The job pre-loads
`already_baseline` (count of `reward_granted = 1`) and increments a
Python-local counter as it stamps. This is correct for a single run —
but if the operator manually re-runs the job after a partial failure
(say after an INSERT but before the stamp UPDATE), the second run's
baseline sees `already + 1` (the in-flight stamp from the first run if
it eventually landed) and shifts every subsequent conversion's reward
tier by one slot. The `revoke_orphan_gift` path catches the obvious
case but not "the same row processed twice across runs with intervening
DB writes from other workers/migrations." Severity: **Medium** — the
"1/5/10" tier math drifts under operator error, and the only test
asserting stacking (`test_stacking_five_conversions_grants_fifth_as_tier_upgrade`)
runs the job once.

Recommendation: store the resolved conversion ordinal on the row at
stamp time (`reward_conversion_number INTEGER` column) and assert
monotonicity. Alternatively, compute the ordinal from
`referrals.id ASC` window-function rather than from a running counter.

**M4. The rate limit on `/api/invite/{code}/accept` is per-IP and
per-email but not per-referrer.** `routes_referrals.py:122-129`. An
attacker with a single referrer account and many emails (or many IPs
via Tor exit nodes) can hammer the email queue: 20/hour from one IP
is enough to send hundreds of invites per day per referrer. There is
no `db.rate_limit_hit(f"invite_accept:referrer:{referrer['id']}", …)`.
Severity: **Medium** — abuse vector against the email reputation /
SendGrid quota, not direct revenue, but enables a "referral spam
flood" exactly like the brief's "payout abuse" warning anticipates.

Recommendation: add a third rate-limit bucket keyed on the referrer
id, e.g. 100/day, before line 131.

### Low

**L1. Public `referred_by` parameter in `public_routes.py` has no
shared validation with the private referral code.** This is the
newsletter waitlist's separate `referral_code` mechanic
(`db.py:308-324`) — different alphabet (`8-char` not `10`), different
table, no link to the private system. Not exploitable on its own, but a
future feature that tries to "promote a newsletter referral into a paid
referral" will likely cross-wire the two and create either double-grant
or attribution loss bugs.

**L2. `_REFERRAL_CODE_ALPHABET` (`db_referrals.py:36`) excludes 0/1/I/O
for visual ambiguity but `get_user_by_referral_code` does **not**
fold these on input.** `db_referrals.py:81-92` only does
`.upper()`. If a customer pastes a link as `…/invite/AbC123O` (with O
instead of zero), the lookup fails. Not a security issue, but the
"casually-copied URL still works" comment at line 83 is misleading.

**L3. `set_leaderboard_participation` updates `leaderboard_handle`
without normalising case or unicode.** `db_referrals.py:333-348`.
The regex `[A-Za-z0-9_-]{3,24}` permits both `Alice` and `alice` as
distinct handles, since the UNIQUE index in the migration is
case-sensitive (no `COLLATE NOCASE`). An attacker can register
`anders` and then someone else can register `Anders`, splitting the
prediction-accuracy reputation of "the" Anders. Severity: **Low** —
display-only deception, no rewards involved, but a real impersonation
risk on the public leaderboard.

**L4. File-naming mismatch.** The user's grep pattern
`gateway/*_routes.py` does not match `routes_referrals.py` (prefix vs
suffix). Any future audit, codemod, or CI gating that uses the
"`*_routes.py` is the routes layer" convention will silently skip this
file. Rename to `referrals_routes.py` or update the convention.

### Informational

**I1. Idempotency comments on `ensure_user_referral_code` and the race
loop.** `db_referrals.py:49-78`. The "re-read on collision" pattern is
sound; the 8-try cap is appropriate given the 32^10 collision space.
No issue, just unusually thorough for a SQLite-backed integer table.

**I2. The leaderboard endpoint is open to every authenticated user,
not just paying subscribers.** `routes_referrals.py:268-281` says
"Paying subscribers only" in the docstring but the code only checks
`_current_user(request)`. If a free user can hold a session at all
(token-based account in early lifecycle), they can hit
`/api/leaderboard`. Not a security/data-leakage issue (the leaderboard
is opt-in and public-handle-only by design) but the docstring's
contract is wrong.

---

## Pipeline correctness summary

End-to-end trace of a successful referral as the code claims it should
work, with the actual code-path next to each step:

| Step | Claimed | Actual |
| ---- | ------- | ------ |
| Inviter shares link | `/invite/{code}` resolves to referrer row | OK — `get_user_by_referral_code`, suspended/deleted excluded |
| Invitee accepts | Pending `referrals` row created, email sent | OK — but no self-referral / per-referrer rate-limit |
| Invitee registers | `referred_user_id` filled in on the pending row | **NOT WIRED** (C1) — no `attach_user_to_referral` call |
| Invitee subscribes | `mark_referral_converted` flips `converted_to_paid` | Only fires if step 3 had wired the row; cross-referrer collision (M2) |
| Daily job grants reward | Insert gift → stamp row → increment credit | OK in normal flow; race window (M1); ordinal drift (M3) |
| Referrer sees reward in `/settings/referrals` | UI reads `referrals` rows | OK |

The path that **does** work today (and the only one that pays out) is
the **share-loop** signup at `server_features.py:1647-1690`: the
`narve_share_attribution` cookie creates a `referrals` row with
`referred_user_id` already populated at creation time, sidestepping
the missing `attach_user_to_referral` call. Every other production
referral is invisible to the reward job.

---

## Out of scope (greppable but unrelated)

- `gateway/public_routes.py` — `referred_by` newsletter waitlist
  mechanic. Separate table (`newsletter_subscribers.referral_code`),
  separate alphabet, separate position-computation logic. No link to
  the paid referral program.
- `gateway/admin_test_emails_routes.py` — admin debug endpoint for
  template rendering. No production data path.
