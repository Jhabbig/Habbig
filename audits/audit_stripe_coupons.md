# Stripe Coupons — Audit

**Date:** 2026-05-15
**Scope:** Stripe coupon-application code path. Verify server-side validation,
one-use-per-customer enforcement, expiration honoring, admin-only mass-grant.
**Branch:** `feature/platform-build` @ `e4c0190`
**Constraint honored:** synchronous bash only; pre-release surface not touched.

---

## TL;DR

**There is no Stripe-coupon code path in this codebase.** The Narve.ai gateway
does not accept, validate, or honor Stripe `coupon` / `promotion_code` objects
anywhere. The closest analogue — admin-granted free subscriptions ("gifts") —
runs entirely server-side via local DB rows in `gifted_subscriptions`, not via
Stripe's coupon API.

Because no coupon code path exists, **the four audit checks are vacuously true
for Stripe coupons**, but the audit found **gaps in the adjacent "gift" surface**
that fail the spirit of each check. Those gaps are listed below.

---

## What was inspected

| Surface | File | Status re: coupons |
|---|---|---|
| Subproduct checkout session | `gateway/subproduct_signup_routes.py` L240-273 | No `discounts`, `coupon`, `promotion_code`, or `allow_promotion_codes` in `session_params`. |
| Trading add-on checkout session | `gateway/billing_routes.py` L1149-1273 | Same — no coupon params passed to `stripe.checkout.Session.create`. |
| Billing portal session | `gateway/billing_routes.py` L1345-1452 | Wraps `stripe.billing_portal.Session.create` only — Stripe-hosted, no coupon plumbing in our code. |
| Stripe webhooks | `gateway/stripe_webhook_routes.py`, `gateway/stripe_webhook_hardening.py` | No `discount`, `coupon`, `promotion`, or `total_discount_amounts` references. |
| Stripe SDK calls | repo-wide grep for `stripe.Coupon`, `stripe.PromotionCode`, `PromotionCode`, `promotion_code`, `coupon_id`, `apply_coupon`, `discounts=` | Zero matches in non-test, non-`extractor.py` code. The one `coupon` regex hit is in `Dashboard-x-truth-research-prediction/app/processing/extractor.py:48` — an unrelated content-classification rule. |
| Frontend | `gateway/static/**.{html,js}` | No coupon-code input field. No `promotion_code` posted from any form. |

**Conclusion: customers cannot present a code, on the client or the server,
that gets validated against Stripe.** Stripe's hosted checkout has
`allow_promotion_codes` *off* by default (we never set it true), so the
Stripe-hosted page also will not accept a promo code.

---

## Adjacent surface: admin-granted "gifts" (the local coupon analogue)

Free access is granted by inserting rows into `gifted_subscriptions`
(`gateway/queries/subscriptions.py:356-415`, schema at `gateway/db.py:392-410`).
Three ingress paths exist:

### 1. Per-user admin grant — `POST /admin/users/{user_id}/grant`
**Location:** `gateway/server.py:6182-6209`
- **Authn/Authz:** `_require_super_admin()` (admin_level >= 2). PASS.
- **CSRF:** enforced by middleware; UI posts via form + `_csrfToken`. PASS.
- **Audit:** `AuditAction.USER_GIFT_SUBSCRIPTION` recorded. PASS.
- **Rate limit:** see `admin_bulk` keyed limiter at `gateway/server.py:5256`; per-admin-email cap.
- **Note:** writes via `db.upsert_subscription(..., source="admin_grant")` —
  not via `create_gift()`. The `gifted_subscriptions` table is used by a
  *different* (currently route-less) flow.

### 2. Referral reward auto-grant — `gateway/db_referrals.py:271-292`
**Path:** referral-conversion job (`gateway/jobs/referral_jobs.py`)
- Calls `insert_referral_gift(...)` server-side; no client input controls it.
- `ends_at = now + months*30*86400` enforced in INSERT — expiration honored.
- Race-loss path `revoke_orphan_gift()` (L295-306) auto-revokes duplicates,
  which is the closest the codebase comes to "one-use-per-customer". PASS for
  intent, though dedupe is **race-detection**, not a unique-index constraint
  on (`user_id`, `referral_id`, `subscription_type`).

### 3. Admin gift UI — `gateway/static/admin.html:1234-1261`
**Frontend exists, backend missing.** The UI `POST`s to
`/admin/users/{uid}/gift` and `GET`s `/admin/api/gifts` / `POST`s
`/admin/gifts/{id}/revoke`. Repo-wide grep for these routes in any `.py`
**returns no matches.** Even the unit tests guard with
`@unittest.skipUnless(_route_exists("/admin/api/gifts"), "route removed")`
(`gateway/tests/test_http_auth.py:176`, `:208`).

Result: clicking "Gift" in the admin UI calls a 404. `create_gift()` from
`gateway/queries/subscriptions.py:356` is exported but **only used in tests**
(`gateway/tests/test_gifts.py`).

---

## Findings against the four audit criteria (interpreted for the gift surface)

### 1. Server-side validation, not client-claimed — PASS (with caveat)
No coupon validation exists because no coupon input is accepted. The gift
admin endpoint that *did* exist would have used a server-only `dashboard_key`
+ `plan` form input under super-admin auth. The dead UI form would have sent
JSON `subscription_type`/`duration`/`enterprise_config` to a route that no
longer exists — so the trust boundary is *over*-restrictive (i.e. fails
closed). **Caveat:** the orphaned UI is a UX defect, not a security defect,
but it should be removed or wired back up.

### 2. One-use-per-customer enforcement — **GAP**
`db.upsert_subscription` (used by `/admin/users/{user_id}/grant`) overwrites
the same (`user_id`, `dashboard_key`) row, so a super-admin re-clicking
"grant" *extends* the duration — there is no idempotency key, no "already
granted" guard, no audit-log dedupe. For referral gifts, `insert_referral_gift`
inserts a new row each call; the only dedupe is the race-detection
`revoke_orphan_gift`. There is **no DB-level UNIQUE constraint** on
`gifted_subscriptions` that ties a gift to a single triggering event
(referral id, promotion id) — a duplicate job execution would mint two
gifts and only retroactively revoke one if the stamp race is detected.

### 3. Expiration honored — PASS
- Subscription expiry: `subscriptions.expires_at` checked everywhere
  entitlement is read; see `gateway/subproduct_access.py` and
  `gateway/queries/subscriptions.py:340-353` (`ends_at > now()` filter).
- Gift expiry: `get_user_active_gifts()` (L400-407) filters
  `is_permanent = 1 OR ends_at IS NULL OR ends_at > ?`. PASS.
- Index `idx_gifts_active ON gifted_subscriptions(revoked, ends_at)`
  supports the active-only query (`gateway/db.py:410`). PASS.

### 4. Admin-only mass-grant — **GAP**
`POST /admin/users/bulk` (`gateway/server.py:6269-6318`) does **not**
include `"grant"` or `"gift"` in the action whitelist
(`promote / demote / suspend / unsuspend / delete` only). Mass-grant is
**not implemented**, which is safe by absence. However:

- The per-user grant endpoint is **not rate-limited per-target** — a
  super-admin script could call `/admin/users/{id}/grant` in a tight loop
  across many users. The `admin_bulk:` rate limit (10 / 5 min / admin)
  only covers the bulk route. **Gap: a single-row grant flood is
  possible.**
- There is no upper bound on `plan` duration: `plan="annual"` → 365 days,
  but any future "lifetime" plan would inherit the same path without a
  cap check. **Gap: no maximum-grant-duration policy.**

---

## Gaps (the deliverable)

1. **No Stripe coupon code path exists.** The audit criteria are vacuously
   satisfied for Stripe coupons themselves — there is nothing to defend.
   If product wants promo codes, this needs design: server-side validation
   via `stripe.PromotionCode.list(active=True, code=...)` + idempotent
   redemption ledger, with `allow_promotion_codes=False` kept off on
   checkout sessions (we already do this).

2. **Orphaned admin gift UI.** `gateway/static/admin.html:1152-1261`
   references `/admin/users/{uid}/gift`, `/admin/api/gifts`, and
   `/admin/gifts/{id}/revoke`. None of these routes are defined in any
   Python file. Either remove the UI or restore the routes — the current
   state is a broken admin button.

3. **No one-use guarantee on `gifted_subscriptions`.** No DB-level UNIQUE
   constraint ties a gift to its triggering event (referral_id /
   admin_action_id). De-duplication is currently a race-detection
   revoke-after-the-fact (`revoke_orphan_gift`), which leaves a brief
   window where a user holds two active rows.

4. **`/admin/users/{user_id}/grant` is not rate-limited per-actor.**
   The `admin_bulk:` limiter only guards `/admin/users/bulk`. The
   single-row endpoint can be called in a loop. Add a per-admin
   `admin_grant:` counter mirroring the bulk one.

5. **No maximum-grant duration policy.** `plan` is mapped 1:1 to a
   duration without a ceiling check. If a `"lifetime"`/`"forever"` plan
   ever ships, the path inherits unbounded grants. Add an explicit
   whitelist of allowed plan strings and a cap.

6. **Tests reference removed routes via `skipUnless`.** Tests at
   `gateway/tests/test_http_auth.py:176, 208` skip silently if the gift
   routes don't exist. This masks the dead-UI defect above. Either
   delete the tests or restore the routes.

---

## Hard-rule compliance

- Synchronous bash only — all commands run foreground, no `&`, no
  `run_in_background=true`. CONFORMS.
- Pre-release surface off-limits — `gateway/static/prerelease.html`
  and `gateway/static/pages/prerelease.css` not opened or modified.
  CONFORMS.
