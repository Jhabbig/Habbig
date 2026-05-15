# Adversarial audit: gift-subscription flow

Date: 2026-05-15
Scope: gift code uniqueness, recipient claim ownership, double-claim
prevention, Stripe-side reconciliation of gift purchases, expiration
handling. Plus access-enforcement coverage of the resulting entitlement.

Files reviewed:

- `/Users/shocakarel/Habbig/gateway/db.py` (schema, exports)
- `/Users/shocakarel/Habbig/gateway/queries/subscriptions.py`
  (`create_gift`, `list_active_gifts`, `get_user_active_gifts`,
  `revoke_gift`, `get_user_intelligence_addon_active`,
  `has_active_subscription`, `get_user_subscription_tier`)
- `/Users/shocakarel/Habbig/gateway/db_referrals.py`
  (`insert_referral_gift`, `mark_referral_reward_granted`,
  `revoke_orphan_gift`)
- `/Users/shocakarel/Habbig/gateway/jobs/referral_jobs.py`
  (`process_referral_rewards`)
- `/Users/shocakarel/Habbig/gateway/jobs/pipeline_jobs.py`
  (account-deletion cascade)
- `/Users/shocakarel/Habbig/gateway/server.py`
  (admin auth helpers, `_require_super_admin`, `_can_manage_user`,
  `admin_grant_subscription`, `admin_toggle_trading_addon`)
- `/Users/shocakarel/Habbig/gateway/server_features.py`
  (share→referral attribution bridge)
- `/Users/shocakarel/Habbig/gateway/static/admin.html`
  (gift modal + REST calls the UI issues)
- `/Users/shocakarel/Habbig/gateway/tests/test_gifts.py`
- `/Users/shocakarel/Habbig/gateway/tests/test_referrals.py`
- `/Users/shocakarel/Habbig/gateway/tests/test_http_auth.py`
- `/Users/shocakarel/Habbig/gateway/security/audit.py`
  (audit-action enum coverage)
- `/Users/shocakarel/Habbig/gateway/billing_routes.py` /
  `/Users/shocakarel/Habbig/gateway/stripe_webhook_routes.py` /
  `/Users/shocakarel/Habbig/gateway/stripe_webhook_hardening.py`
  (Stripe surface; confirmed no gift purchase path)
- `/Users/shocakarel/Habbig/gateway/migrations/184_explain_audit_indexes.py`
- `/Users/shocakarel/Habbig/gateway/exports/generator.py`

No code changes. Findings only.

---

## Threat-model summary

`gifted_subscriptions` is **not** a user-purchasable gift product. There
is no public "buy a gift for a friend" flow, no claim code, no Stripe
checkout path, and no recipient-claim handshake. Two grant paths exist:

1. **Admin-issued gift** — UI in `static/admin.html` collects
   subscription_type, duration, optional enterprise config, optional
   internal notes, and POSTs to `/admin/users/{user_id}/gift`.
2. **Referral reward gift** — `jobs.referral_jobs.process_referral_rewards`
   (daily cron) auto-inserts a `gifted_subscriptions` row when a
   referrer hits a milestone conversion count.

Therefore the audit prompt's headline concerns (gift-code uniqueness,
recipient claim ownership, double-claim prevention, Stripe reconciliation
of gift purchases) **do not directly apply**: there is no code, no claim,
and no Stripe gift SKU. The genuine attack surface lives in:

- the missing HTTP handler that the admin UI POSTs to
  (`/admin/users/{user_id}/gift`, `/admin/api/gifts`,
  `/admin/gifts/{id}/revoke`),
- the gift-row insertion ACL,
- the expiration filter in `get_user_active_gifts`,
- the *non-coverage* of paid gift tiers in subscription-tier and
  has-active-subscription resolution,
- the referral reward job's race-recovery branch.

Findings below are framed against the actual flow.

## Severity legend

- C  Critical (immediate production exploit, paywall bypass, or RCE)
- H  High    (exploit needs minor preconditions; significant impact)
- M  Medium (defence-in-depth gap; bypassable with care or staging-only)
- L  Low    (hygiene / hardening / contradicted comments)
- I  Informational

## Severity counts

- Critical: 0
- High:     3
- Medium:   4
- Low:      4
- Informational: 4

Total: 15 findings.

---

## Top 3 (rank-ordered)

1. **H-1** — Paid gift tiers (`pro_monthly`, `pro_annual`,
   `trader_monthly`, `enterprise`) are **silently ignored by every
   access-enforcement path except Intelligence add-on**. A gift inserted
   with `subscription_type = 'pro_monthly'` writes a row but
   `has_active_subscription`, `has_any_active_subscription`,
   `get_user_subscription_tier`, and `get_user_active_subproducts`
   never consult `gifted_subscriptions`. The recipient receives no
   product access until/unless a parallel `subscriptions` row is also
   inserted — which the gift code does not do. NARVE_SECURITY_AUDIT.md
   lines 368/585 assert "gift subscription enforcement: yes" — that
   assertion is wrong for everything except `intelligence_addon` and
   the `enterprise_config.intelligence_addon_included` flag.

2. **H-2** — `get_user_active_gifts`
   (`queries/subscriptions.py:364`) treats a row as active when
   `is_permanent = 1 OR ends_at IS NULL OR ends_at > now`. The
   `ends_at IS NULL` disjunct means any row whose `ends_at` was left
   NULL — regardless of `is_permanent` — never expires. Both
   `create_gift` and `insert_referral_gift` accept `ends_at=None`, so
   a buggy caller (or a future admin endpoint that forgets to compute
   `ends_at`) produces an irrevocable lifetime grant without anyone
   ticking the `is_permanent` flag. Combined with H-1, this is currently
   only exploitable for the Intelligence add-on path, but the broken
   contract is a foot-gun for any future enforcement glue.

3. **H-3** — The admin UI in `static/admin.html` calls three REST
   endpoints that **do not exist in the Python code**:
   `POST /admin/users/{uid}/gift`, `GET /admin/api/gifts`, and
   `POST /admin/gifts/{id}/revoke`. `tests/test_http_auth.py:176,208`
   conditionally skips on `_route_exists("/admin/api/gifts")` — that
   skip is firing in production, meaning the dangling UI surface ships
   with no negative-auth test. The actual `/admin/users/{user_id}/grant`
   endpoint (server.py:6181) writes to the `subscriptions` table, not
   `gifted_subscriptions`, and the audit-log action
   `USER_GIFT_SUBSCRIPTION` (security/audit.py:38) is emitted from it —
   which means the audit trail labels regular admin-grants as "gifts",
   blurring the two products in any compliance review. Together: the
   gift UI is dead, the audit log is mislabelled, and no http-level
   test prevents resurrecting the route without the correct
   `_require_super_admin` gate.

---

## High findings

### H-1 — Paid gift tiers grant nothing

**Location:** `queries/subscriptions.py:29` (`has_active_subscription`),
`queries/subscriptions.py:424` (`get_user_subscription_tier`),
`queries/subscriptions.py:514` (`has_any_active_subscription`),
`queries/subscriptions.py:539` (`get_user_active_subproducts`).

`get_user_active_gifts` is read from exactly one place outside tests:
`get_user_intelligence_addon_active`
(`queries/subscriptions.py:382-403`). Every other entitlement path
queries the `subscriptions` table directly. Consequences:

- A super admin gifting `subscription_type="pro_monthly"` via the
  intended (currently dead) flow grants nothing — the recipient still
  sees the paywall.
- The referral reward job (`jobs/referral_jobs.py:177-198`) inserts
  `subscription_type=reward["tier"]` (values: `"pro"` or `"trader"`,
  resolved at `referral_jobs.py:120-128`) and the credit display
  counter is bumped (`add_referral_credit_months`), but the user is
  not actually upgraded. The "1 month free" advertised in the
  referrals UI is unenforced.
- `enterprise_config` containing `max_api_calls_per_day`, `max_topics`,
  `api_rate_limit`, `allowed_features`, `trading_addon_included` is
  persisted but **only `intelligence_addon_included` is ever read**
  (`queries/subscriptions.py:401`). The other five enterprise knobs
  are write-only.

**Recommendation:** Either route gift grants through `upsert_subscription`
to land in the canonical table (with `source="gift"`), or extend each
entitlement helper to OR `gifted_subscriptions` into the existence
check. The current half-implementation is worse than either choice.

### H-2 — `ends_at IS NULL` defeats expiration filter

**Location:** `queries/subscriptions.py:367-371`.

```
"WHERE user_id = ? AND revoked = 0
 AND (is_permanent = 1 OR ends_at IS NULL OR ends_at > ?)"
```

A row inserted with `is_permanent=False, ends_at=None` is still
returned as active. `create_gift` (`subscriptions.py:320-350`) accepts
both parameters independently — a buggy admin form, a future
import-script, or a test fixture that omits `ends_at` produces a
forever-grant without anyone setting `is_permanent`. The intent (from
the schema columns and the modal UI's "Lifetime" radio) is that
permanence is opt-in via `is_permanent`; the disjunction makes it
opt-out.

**Recommendation:** Change to
`(is_permanent = 1 OR (ends_at IS NOT NULL AND ends_at > ?))` and
add a NOT-NULL CHECK constraint when `is_permanent = 0`.

### H-3 — Dangling gift admin endpoints

**Location:** `static/admin.html:1152,1170,1253` versus all `@app.post`
/ `@app.get` decorators in `server.py`.

The frontend calls:
- `GET /admin/api/gifts` — to list active gifts
- `POST /admin/users/{uid}/gift` — to issue a gift
- `POST /admin/gifts/{id}/revoke` — to revoke

None of these routes are registered. `test_http_auth.py:176,208`
uses `_route_exists` as a skip-guard, so the absence ships green.
The visible admin tab + modal load a "Loading gifts…" spinner that
never resolves, then a `fetch` to a 404. The risk is two-fold: (a)
the dead UI invites a future PR to "fix the missing endpoint" without
re-discovering the necessary `_require_super_admin` + CSRF + audit
glue, and (b) the action enum `USER_GIFT_SUBSCRIPTION` is currently
fired by `admin_grant_subscription` (server.py:6200) which writes to
`subscriptions`, not `gifted_subscriptions` — meaning the audit log
already calls regular subscription grants "gifts" and any future real
gift endpoint cannot be distinguished in the audit trail without an
enum split.

**Recommendation:** Either remove the dead UI surface or land the
real endpoints with `_require_super_admin`, csrf middleware coverage,
the per-admin rate-limit (already enforced by `_require_admin_user`),
and a dedicated `USER_REVOKE_GIFT` action (which already exists in
the enum at `security/audit.py:39` but is never emitted from any
call site).

---

## Medium findings

### M-1 — Account deletion deletes paid grants instead of preserving for refund/audit

**Location:** `jobs/pipeline_jobs.py:84`.

```
c.execute("DELETE FROM gifted_subscriptions WHERE user_id = ?", (user_id,))
```

The GDPR-style anonymisation routine **physically deletes** all of a
user's gift rows. Comment two lines down (line 88) explicitly says
"subscriptions, analytics_events, user_bet_history retained — financial
/ research records — retained for legal compliance". Gift grants are
also a financial/audit-relevant record (enterprise partnerships,
referral payouts, press comps) and should be retained or soft-tombstoned
the same way `subscriptions` is. Currently a deleted account erases
the evidence of any paid press/partnership grant the company issued.

**Recommendation:** Replace the `DELETE` with `UPDATE gifted_subscriptions
SET revoked = 1, revoked_at = ?, internal_notes = COALESCE(internal_notes,
'') || ' [account deleted; auto-revoked]' WHERE user_id = ?`. Matches the
existing orphan-revoke pattern in `db_referrals.revoke_orphan_gift`.

### M-2 — Referral reward race-recovery uses two separate transactions

**Location:** `jobs/referral_jobs.py:180-233`.

The insert (`INSERT INTO gifted_subscriptions ...`) commits in its own
`with db.conn() as c:` block, then `mark_referral_reward_granted` opens
a second transaction. If the worker crashes between the two commits,
the gift exists with no referral stamp and the next run will:
- not see the referral row in `list_pending_reward_referrals` (it's
  still `reward_granted=0` so it IS picked up again),
- compute the same milestone,
- insert a SECOND gift,
- only THEN attempt to stamp.

Result: the orphan-revoke branch (line 217) cleans up the second
insertion's orphan, but the FIRST insertion's orphan stays active
forever. Double-grant on crash.

Even without a crash: the design comment at line 12-19 of
`referral_jobs.py` argues a single daily batch gives natural
serialization, but `process_referral_rewards` is a `@register_job`
that can also be invoked on demand (admin "run now" pattern). Two
concurrent invocations race on the same pending row.

**Recommendation:** Wrap the insert + stamp in a single `db.conn()`
context. If `mark_referral_reward_granted` returns rowcount=0, raise
to roll back the insert. Drop the post-hoc orphan-revoke branch.

### M-3 — `revoke_gift` does not check `_can_manage_user`

**Location:** `queries/subscriptions.py:374-379`.

```python
def revoke_gift(gift_id: int, admin_id: int) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE gifted_subscriptions SET revoked = 1, revoked_at = ?, "
            "revoked_by_admin_id = ? WHERE id = ?",
            (int(time.time()), admin_id, gift_id),
        )
```

The DB helper takes an `admin_id` for the audit column but performs no
authorisation check. A level-1 admin could revoke a gift previously
issued by a level-2 super admin to another super admin if a future
endpoint forgets the `_can_manage_user` step. `admin_toggle_trading_addon`
(server.py:6211-6233) uses `_can_manage_user` correctly; the gift
equivalent depends entirely on the caller. Combined with H-3 (the
endpoint doesn't exist yet), this is a foot-gun waiting to be loaded.

**Recommendation:** Require the helper to take the target's row, not
just an `admin_id` token; or document that callers MUST gate with
`_can_manage_user`. Either is fine — the current "trust the caller"
contract is the weakest option.

### M-4 — Enterprise config blob is unvalidated free-form JSON

**Location:** `queries/subscriptions.py:330-348`.

`_json.dumps(enterprise_config)` accepts any dict, with no schema
check, length limit, or known-key allowlist. The example UI inputs
(`gift-ent-api`, `gift-ent-topics`, `gift-ent-ratelimit`,
`gift-ent-trading`, `gift-ent-intelligence`) imply a fixed shape, but
the DB layer happily stores unbounded JSON. A future endpoint that
forwards `request.json()` straight through could store an arbitrarily
large blob (no `LENGTH` limit on the TEXT column), and the only path
that reads it (`get_user_intelligence_addon_active`, line 397-401)
wraps `_json.loads` in `except Exception` and silently degrades — so
malformed JSON is masked, not surfaced.

**Recommendation:** Define a typed schema (TypedDict / Pydantic) at
the call boundary, validate before insert, cap the column with
`CHECK(LENGTH(enterprise_config) < 8192)` in the migration, and log
rather than swallow JSON parse errors.

---

## Low findings

### L-1 — `audit.AuditAction.USER_REVOKE_GIFT` defined but never emitted

`security/audit.py:39` declares the action; no call site emits it.
A revoke happens silently in `revoke_gift` with no `_audit.log_action`
call. Gift grants log `USER_GIFT_SUBSCRIPTION` via the (mislabelled —
see H-3) `admin_grant_subscription` endpoint; revokes log nothing.

### L-2 — Hard-coded `30 * 86400` everywhere

`queries/subscriptions.py:283`, `db_referrals.py:283`,
`jobs/referral_jobs.py:179`, `server.py:6217` all spell out
`months * 30 * 86400`. The constant 30-day-month assumption silently
drifts: gifting "1 month" actually grants 30 calendar days, which is
27-31 days short of "1 month" depending on starting date. Inconsistent
with Stripe billing-cycle semantics elsewhere in the codebase.

### L-3 — `gifted_subscriptions` rows are never compacted post-revoke

Schema has no `expired` or `terminated_at` column; revoked rows
accumulate forever. The 184_explain_audit_indexes migration explicitly
calls out a partial index `WHERE revoked = 0`, acknowledging the
unbounded growth, but no archive / purge job exists. Operationally
fine today, will be a slow degrade once the user base grows.

### L-4 — `gifted_by_admin_id` is `ON DELETE SET NULL`

`db.py:389`. Acceptable for FK integrity, but combined with M-1
(account-delete erases the gift row entirely on the *recipient* side),
the join in `list_active_gifts` (`subscriptions.py:355-360`) will
display "granted by: unknown" for any gift whose granting admin's
account was later deleted. Tolerable, but the asymmetry between
recipient-CASCADE and admin-SET-NULL deserves a comment.

---

## Informational

### I-1 — No public gift-purchase product exists

Confirmed by exhaustive search of `gateway/` for `gift_code`,
`gift_card`, `claim`, `recipient`, `gift.*stripe`, `stripe.*gift`,
and `purchase.*gift`: zero matches outside the admin/referral context
documented above. The audit prompt's framing (uniqueness, claim
ownership, double-claim, Stripe reconciliation) is therefore
inapplicable. If a gift-purchase product is on the roadmap, this audit
is a useful baseline — the existing schema would need at minimum a
`claim_code` UNIQUE column, a `claimed_by_user_id` separate from
`user_id`, a `claimed_at` timestamp, and a Stripe webhook handler
that resolves `checkout.session.completed` with a gift-product price
ID into a `gifted_subscriptions` insert.

### I-2 — Share-attribution → referral bridge exists but isn't gift-aware

`server_features.py:1647-1690` links a share-click cookie to a new
signup and creates a referral row. The downstream reward path
ultimately writes a `gifted_subscriptions` row. If the gift entitlement
path is later fixed (H-1), this bridge becomes a higher-leverage
target for spammers / multi-account farms — flag for re-audit when
H-1 is closed.

### I-3 — `internal_notes` is operator-controlled, no XSS sanitisation

`internal_notes` is rendered in the admin gifts list (per
`subscriptions.py:355-360` join, surfaced via the missing
`/admin/api/gifts` endpoint, then `static/admin.html:1152-1167` would
inject it into the DOM). If that endpoint is brought back, ensure
the JS path uses textContent rather than innerHTML for this field.
Currently moot (route doesn't exist).

### I-4 — Tests cover the DB layer well but not the policy layer

`tests/test_gifts.py` exercises insert / revoke / list / addon-flag
correctly. There is no test asserting that
`has_active_subscription("pro_monthly_gift_user", "polymarket")` returns
True, which is precisely the H-1 gap. A test would have caught it.

---

## Cross-cutting recommendation

The flow is in an awkward partly-built state: schema, DB layer, admin
UI, audit-enum, and referral-job integration all exist, but the HTTP
boundary and the access-enforcement boundary are both incomplete. The
honest move is either:

1. **Finish it** — land the three missing routes with the same auth /
   CSRF / rate-limit gates as `admin_grant_subscription`, fix H-1 by
   wiring `get_user_active_gifts` into `has_active_subscription` and
   `get_user_subscription_tier`, fix H-2's NULL-ends-at trap, and add
   M-2's transactional fix.
2. **Tear it out** — remove `static/admin.html`'s gift tab, drop the
   four exported helpers from `db.py`, deprecate the table in a no-op
   migration, and tell super admins to use `/admin/users/{id}/grant`
   for everything. The referral reward job would need to be re-pointed
   at `upsert_subscription` with `source="referral_reward"`.

Choosing the "in between" status quo means every future audit will
keep flagging the same H-1/H-2/H-3 cluster.
