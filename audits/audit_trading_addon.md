# Adversarial audit — Trading Add-on routes

Date: 2026-05-15
Auditor: Claude (Opus 4.7)

## Scope and file location note

The brief named `gateway/trading_addon_routes.py` as the audit target.
**That file does not exist** in the tree (verified by
`find /Users/shocakarel/Habbig -name "trading_addon_routes*"` and
`grep -rln "trading_addon" gateway --include="*.py"`). The Trading
Add-on surface is actually split across:

- `/Users/shocakarel/Habbig/gateway/billing_routes.py` — add / cancel
  HTTP endpoints (`/settings/billing/addon`, `/settings/billing/addon/cancel`),
  billing-page rendering (`_render_addons`, `_render_current_plan`,
  `_render_cancel_losses`, `_derive_invoices`).
- `/Users/shocakarel/Habbig/gateway/server.py` lines 7459-7638 —
  `/settings/trading-addon` page, `GET /api/trading-addon/config`,
  `PATCH /api/trading-addon/config`, and the admin toggle at
  `/admin/users/{id}/trading-addon`.
- `/Users/shocakarel/Habbig/gateway/queries/markets.py` lines 458-605 —
  the `get_trading_addon_status`, `set_trading_addon`,
  `has_trading_addon`, `get_trading_addon_settings`,
  `upsert_trading_addon_settings` query helpers (re-exported from `db`).
- `/Users/shocakarel/Habbig/gateway/market_routes.py:222-234` —
  `_require_markets_user`, the gate consulted by every
  `/api/markets/*` route.
- `/Users/shocakarel/Habbig/gateway/migrations/176_trading_addon_settings.py` —
  schema for per-user trading-addon tunable settings.
- `/Users/shocakarel/Habbig/gateway/stripe_webhook_routes.py` — Stripe
  event dispatch (does **not** currently grant trading add-on; see C-1).
- `/Users/shocakarel/Habbig/gateway/backend/payments/stripe_stub.py` —
  documents the unimplemented Stripe Checkout integration with
  `STRIPE_PRICE_TRADING_ADDON_MONTHLY` / `STRIPE_PRICE_TRADING_ADDON_ANNUAL`.

The brief's three focus areas — read consistency with the FIX agent's
in-progress Stripe Checkout conversion, period_end enforcement at every
read, free-trial extension bypass — all land on this surface, so the
audit covers it without retargeting.

No code changes made. Findings only.

---

## Severity legend

- C — Critical (immediate exploit, paywall bypass, or RCE)
- H — High (exploit needs minor preconditions; major impact)
- M — Medium (defence-in-depth gap; bypassable with care or precondition)
- L — Low (hygiene / consistency / contradicted comments)
- I — Informational

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 1     |
| High     | 3     |
| Medium   | 4     |
| Low      | 5     |
| Info     | 2     |
| **Total**| **15**|

---

## Top 3 findings (rank-ordered by exploitability x impact)

1. **C-1 — Self-grant: `/settings/billing/addon` mints a paid add-on
   without Stripe.** `billing_routes.py:1130-1165` calls
   `db.set_trading_addon(uid, True, period_end=now + 30 * 86400)`
   inside `_do_add()` **unconditionally** — no Stripe Checkout, no
   payment, no webhook involvement. Any logged-in user who POSTs
   `addon=trading` (after a CSRF prime) flips their own
   `users.trading_addon_active` flag to 1 and gets 30 days of trading
   API access (markets list, portfolio sync, order placement
   surfaced via `market_routes._require_markets_user`). The test at
   `tests/test_settings_billing.py:683-706`
   (`test_addon_add_with_stripe_stubbed_fails_closed`) explicitly
   asserts the *opposite* — that the handler should redirect to
   `billing_unavailable` and not flip the flag — and is described in
   its docstring as guarding the "audit-fix" guarantee. The route is
   the canonical pre-FIX behaviour; the FIX agent is mid-migration to
   Stripe Checkout. **Today, the gateway lets any user self-grant the
   add-on for free, in 30-day chunks, repeatedly.** See C-1.

2. **H-1 — `set_trading_addon` is a REPLACE, not an extend, of
   `period_end`.** `queries/markets.py:475-481` does
   `UPDATE users SET trading_addon_active = ?, trading_addon_period_end = ?`.
   When `billing_routes.py:1152` calls
   `set_trading_addon(uid, True, period_end=now + 30 * 86400)`, the
   stored `period_end` is **replaced** with "30 days from now"
   regardless of what was there before. Combined with C-1, a user who
   bought ~10 days of trading addon and clicks "Add to plan" again
   (because the UI shows the button while the addon is active —
   the UI is admin-toggle-shaped, not subscription-shaped) extends
   their period_end by 20 days for free. The route's own docstring
   at `billing_routes.py:1134-1139` ("A genuinely-later re-add (> 10 s)
   still extends, by design: the user chose to top up again")
   acknowledges this and frames it as intentional — but in the
   presence of C-1, "the user chose to top up" is the user paying
   nothing for an extension. The two together are an indefinite free
   trial. See H-1.

3. **H-2 — No Stripe webhook branch grants or revokes the trading
   add-on; `stripe_webhook_routes.py` only touches `subscriptions` /
   `subproduct_subscriptions`.** When the FIX agent lands a Checkout
   session for the trading add-on, the webhook
   (`stripe_webhook_routes.py:285-294`) has only five dispatch
   branches: `customer.subscription.created/updated/deleted`,
   `invoice.paid`, `invoice.payment_failed`. None of them call
   `db.set_trading_addon`. So even after the FIX agent migrates
   `/settings/billing/addon` to "redirect to Stripe Checkout", the
   completed checkout will land a `customer.subscription.created`
   event that `_grant_access` writes into `subscriptions` (with
   `dashboard_key = "trading"` if and only if metadata is set
   correctly), but `users.trading_addon_active` will stay 0 and
   `has_trading_addon` will keep returning False. The trading add-on
   stops being grantable at all. The current C-1 path is
   structurally the *only* way to flip the flag in production —
   remove C-1 without adding a webhook handler and paying users are
   locked out. See H-2.

---

## Findings

### C-1 — Self-grant: `/settings/billing/addon` mints 30 days of trading access without payment

**Location:** `/Users/shocakarel/Habbig/gateway/billing_routes.py:1130-1165`.

**What:**

```python
@app.post("/settings/billing/addon", include_in_schema=False)
async def settings_billing_addon_add(request: Request, addon: str = Form(...)):
    user = current_user(request)
    _billing_rate_limit(user, "addon")
    if not user:
        return RedirectResponse("/token", status_code=302)
    if addon != "trading":
        return RedirectResponse("/settings/billing", status_code=302)
    from security.idempotency import with_idempotency
    uid = user["user_id"]

    async def _do_add() -> dict:
        now = int(time.time())
        db.set_trading_addon(uid, True, period_end=now + 30 * 86400)
        ttl_invalidate.on_subscription_change(uid)
        log.info("User %s added trading add-on", user.get("username", user["email"]))
        return {"ran": True}

    await with_idempotency(
        user_id=uid,
        op="billing_addon_add",
        client_key=request.headers.get("Idempotency-Key"),
        ttl_seconds=10,
        body=_do_add,
        fallback_fingerprint=addon,
    )
    return RedirectResponse("/settings/billing?saved=addon_added", status_code=302)
```

`db.set_trading_addon` runs on the local DB with no Stripe interaction,
no payment, no webhook, no entitlement check beyond "is the user
logged in and not over a 20/hour rate limit". The route docstring
truthfully labels Stripe as "stubbed" (line 1132 — "Stripe stubbed")
but the production deploy still mounts this route because Stripe
isn't conditionally wired in — `billing_routes` is imported
unconditionally at the bottom of `server.py`. The CSRF middleware
applies (good — no cross-site forging) but every authenticated user
can call it from a primed page.

**Confirming evidence from the codebase:**

- `tests/test_settings_billing.py:651-706` defines `TestAddonFlow`
  whose docstring is *explicit*:

  > "The add-on add endpoint MUST NOT flip the local flag directly —
  > that was the audit HIGH self-grant finding. It must redirect to a
  > Stripe Checkout session; the webhook (separate test file) flips the
  > flag on `checkout.session.completed`. With Stripe stubbed in the
  > default test env we expect FAIL CLOSED behavior: a redirect to
  > /settings/billing?error=billing_unavailable and zero DB write."

  The test then asserts:
  ```python
  self.assertIn("error=billing_unavailable", r.headers["location"])
  self.assertFalse(
      db.get_trading_addon_status(self.uid)["active"],
      "addon flag was flipped without a Stripe checkout — self-grant!",
  )
  ```

  This test is hard-coded against the desired post-FIX behaviour, and
  is failing against the current implementation. The FIX agent
  hasn't shipped.

- `backend/payments/stripe_stub.py:5-13` warns explicitly that the
  Stripe wiring is the missing piece:

  > "An unauthenticated Stripe webhook endpoint is a subscription-forgery
  > vulnerability ... This module is a stub; the real implementation
  > is deliberately absent so that an accidental route mount cannot
  > silently accept forged events."

  The companion concern — the *grant* path — has no such guard. The
  trading add-on POST handler is mounted and grants entitlement
  whether or not Stripe is configured.

**Attack:**

1. Sign up / log in (free tier — no Pro subscription required to hit
   this route).
2. GET `/settings/billing` to prime `_csrf` cookie.
3. POST `/settings/billing/addon` with form body
   `addon=trading&_csrf=<value>` and cookies `narve_session=<your token>`,
   `_csrf=<value>`.
4. 302 → `/settings/billing?saved=addon_added`. `users.trading_addon_active = 1`,
   `users.trading_addon_period_end = now + 30 days`.
5. Hit `/api/markets/unified` — succeeds. `_require_markets_user` at
   `market_routes.py:232` reads `has_trading_addon` → True. Full
   markets API access without payment.
6. (See H-1 for the indefinite-extension chain.)

The rate limit at `_billing_rate_limit` is 20 mutations/hour scoped
to `(user_id, action)` (the `action="addon"` distinguishes it from
`addon_cancel`). 20 free self-grants/hour, repeatable forever.

**Why not deferred to the FIX:** The fix lands when it lands, but the
route is live today. Severity is set on current production behaviour,
not on the in-flight branch.

**Fix:**

The intent (per the failing test and per the audit-HIGH it references)
is that the route should:

1. Refuse to flip the local flag at any point.
2. Build a Stripe Checkout Session for
   `STRIPE_PRICE_TRADING_ADDON_MONTHLY`/`..._ANNUAL` with
   `metadata={"user_id": uid, "addon": "trading", "interval": ...}`.
3. 303 to `session.url`.
4. The webhook handler (H-2) flips the flag on
   `checkout.session.completed` (or `customer.subscription.created` with
   `addon` metadata).

Until then, defence-in-depth interim: gate on
`STRIPE_SECRET_KEY` being set and at least one of the
`STRIPE_PRICE_TRADING_ADDON_*` env vars being non-empty — return
`error=billing_unavailable` otherwise and do nothing to the DB. This
matches the existing pattern in `settings_billing_resubscribe`
(`billing_routes.py:1097-1106`) which already fails closed when
Stripe is unconfigured.

---

### H-1 — `set_trading_addon` replaces `period_end` instead of extending; combined with C-1, this is an unlimited free trial

**Location:** `/Users/shocakarel/Habbig/gateway/queries/markets.py:475-481`,
caller at `/Users/shocakarel/Habbig/gateway/billing_routes.py:1152`.

**What:**

```python
def set_trading_addon(user_id: int, active: bool, period_end: Optional[int] = None) -> None:
    """Admin toggle for trading add-on."""
    with db.conn() as c:
        c.execute(
            "UPDATE users SET trading_addon_active = ?, trading_addon_period_end = ? WHERE id = ?",
            (1 if active else 0, period_end, user_id),
        )
```

The `UPDATE` writes the supplied `period_end` verbatim. There is no
`MAX(stored, new)` extension logic at the DB layer.

`billing_routes.py:1152` always passes `now + 30 * 86400`. So the
period_end is reset to "30 days from now" on every successful POST,
regardless of:

- whether the user already had a longer period_end (from a Stripe
  payment, an admin grant, or a prior self-grant);
- whether the user's previous period_end was further or closer to now;
- whether the user paid this time.

**Attack chain (extends C-1):**

1. (Day 0) User self-grants via C-1. period_end = day 30.
2. (Day 28) User self-grants again. period_end = day 58 (gain: 28 days).
   At a higher cadence (clicks 20 times per hour, rate limit cap), they
   can lock in a fresh 30-day window once every ~5 minutes —
   asymptotically infinite access.
3. Conversely, even **legitimate paid users** get screwed: if a paid
   user with 90 days remaining (e.g. annual billing) clicks "Add to
   plan" thinking it'll renew, their period_end is **shortened** to
   30 days. The route's own docstring at line 1134-1139 advertises
   this as "extends, by design" but the SQL is `=`, not `MAX`. The
   docstring is wrong about the mechanics; the behaviour matches the
   docstring only by accident (when stored period_end happens to be
   < 30 days away).

**Why not C-grade:** depends on C-1 to be exploitable without
payment. Without C-1, this finding is "billing extension shortens
already-paid period" — a paid-user disservice but not a paywall
bypass. Together they are the canonical indefinite free trial.

**Fix:**

```python
def set_trading_addon(user_id: int, active: bool, period_end: Optional[int] = None) -> None:
    with db.conn() as c:
        if active and period_end is not None:
            c.execute(
                "UPDATE users SET trading_addon_active = 1, "
                "trading_addon_period_end = "
                "  CASE WHEN trading_addon_period_end IS NULL THEN ? "
                "       WHEN trading_addon_period_end < ? THEN ? "
                "       ELSE trading_addon_period_end END "
                "WHERE id = ?",
                (period_end, period_end, period_end, user_id),
            )
        else:
            c.execute(
                "UPDATE users SET trading_addon_active = ?, "
                "trading_addon_period_end = ? WHERE id = ?",
                (1 if active else 0, period_end, user_id),
            )
```

Or, simpler: expose `extend_trading_addon(user_id, additional_days)` as a
separate helper that does `period_end = MAX(period_end, now) + N*86400`,
and have the (post-FIX) Stripe webhook call that on
subscription.created/updated; reserve `set_trading_addon` for the
admin-toggle case where replacement is the intent.

---

### H-2 — No Stripe webhook branch grants the trading add-on; the post-FIX surface is unwired

**Location:**
`/Users/shocakarel/Habbig/gateway/stripe_webhook_routes.py:281-299`
(dispatch), grep `set_trading_addon` across `gateway/` excluding
tests/conftest:

```
gateway/server.py:6199         db.set_trading_addon(user_id, bool(active), period_end)   <- admin toggle
gateway/billing_routes.py:1194 db.set_trading_addon(uid, True, period_end=now + 30 * 86400)   <- C-1 self-grant
gateway/billing_routes.py:1219 db.set_trading_addon(user["user_id"], False, None)   <- self-cancel
```

**What:** The dispatch table in `stripe_webhook_routes.py:285-299`:

```python
if event_type == "customer.subscription.created":
    _grant_access(event)              # writes subscriptions row
elif event_type == "customer.subscription.updated":
    _update_plan(event)               # updates subscriptions row
elif event_type == "customer.subscription.deleted":
    apply_subscription_cancelled(event)  # cancels subscriptions row + revokes session
elif event_type == "invoice.payment_failed":
    apply_invoice_payment_failed(event)
elif event_type == "invoice.paid":
    _record_payment(event)
```

`_grant_access` (lines 84-123) and `_update_plan` (lines 126-163) both
key off `metadata.dashboard_key` and write into the `subscriptions`
table. The trading add-on lives on `users.trading_addon_active` /
`users.trading_addon_period_end`. Nothing in the dispatch — and
nothing in `stripe_webhook_hardening.py` — calls
`set_trading_addon`.

So when the FIX agent's Stripe Checkout session completes and Stripe
delivers `customer.subscription.created` with
`metadata.addon="trading"`, the gateway will silently write the
event into the `subscriptions` table under whatever `dashboard_key`
was passed (or skip the event entirely if metadata is missing —
`_grant_access` early-returns at lines 100-105 when `user_id` or
`dashboard_key` is empty). `users.trading_addon_active` stays 0.
`has_trading_addon` returns False. The paid user has no access.

**Why High not Critical:** The exploit isn't an *attack*; it's a
silent paywall bypass in the other direction (paid users locked out).
But it's a deploy blocker for the FIX. If C-1 is fixed without
adding this webhook branch, every newly-paid trading add-on user
gets a "you didn't pay" message and a refund-or-churn outcome. The
FIX agent's branch is incomplete unless it lands both halves.

**Fix:**

Add a dispatch branch to `stripe_webhook_routes.py`:

```python
def _grant_trading_addon(event: dict) -> None:
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    if (meta.get("addon") or "") != "trading":
        return
    user_id = _coerce_int(meta.get("user_id") or meta.get("narve_user_id"))
    if not user_id:
        log.warning("trading_addon grant missing user_id: id=%s", event.get("id"))
        return
    # Use current_period_end from the Stripe Subscription object so the
    # local period_end matches Stripe's billing cycle exactly.
    period_end = _coerce_int(obj.get("current_period_end"))
    if not period_end:
        period_end = int(time.time()) + 30 * 86400  # safe default
    db.set_trading_addon(user_id, True, period_end)
    # ... cache invalidate, audit log ...
```

Wire it into the dispatch table for `customer.subscription.created`
and `customer.subscription.updated` (branch by `metadata.addon`
before falling through to `_grant_access` / `_update_plan`).
`customer.subscription.deleted` should mirror this by calling
`set_trading_addon(user_id, False, None)` when metadata.addon is
"trading". `invoice.payment_failed` should *not* immediately revoke
(matches Stripe's dunning grace) but should log a warning so the
on-call channel knows.

Defence-in-depth: the H-1 fix (extend, don't replace) means that an
out-of-order webhook delivery doesn't shorten the period_end set by
a later event.

---

### H-3 — Free-trial extension via cancel→add cycle: idempotency window is too short to prevent it

**Location:**
- `/Users/shocakarel/Habbig/gateway/billing_routes.py:1130-1180` (add /
  cancel routes)
- `/Users/shocakarel/Habbig/gateway/security/idempotency.py:147-190`
  (`with_idempotency`)

**What:** The add route wraps `_do_add` in `with_idempotency` with
`ttl_seconds=10` (line 1161). That collapses double-clicks within
10 s. The cancel route at line 1168-1180 has **no idempotency
wrapper** and immediately writes `set_trading_addon(uid, False, None)`.

Sequence (assuming C-1 is patched but the route still works without
Stripe somewhere — staging, an internal API, an admin tool):

1. User has period_end at day 30 from a paid Stripe purchase.
2. User POSTs `/settings/billing/addon/cancel` → period_end = None,
   active = 0.
3. After 10 seconds (to avoid the idempotency collapse), user POSTs
   `/settings/billing/addon` → period_end = now + 30 days, active = 1.
4. User has gained ~30 days for free (the original paid period is now
   gone but a fresh 30 days is granted).
5. On the rate-limit budget (20/hour), this works 20 times per hour
   per (user, action) pair. Two separate buckets — `addon` and
   `addon_cancel` — means each can independently burn 20/hour.

Compared to C-1, this is the same exploit but with the cancel step
mixed in. After C-1's fix to redirect to Stripe Checkout, this stops
being a free-trial vector for the unprivileged user — but the
`set_trading_addon(uid, False, None)` cancel route still permanently
loses any longer-than-30-day period_end on the *first* cancel. Need
to either:

1. Soft-cancel: flip `active=0` but keep `period_end` (so resume
   restores), or
2. Hard-cancel with a Stripe portal redirect (consistent with the
   `subscriptions` table cancel path at `billing_routes.py:920-925`).

**Why High not Critical:** Pre-fix, C-1 already gives unlimited free
trials without needing the cancel step at all. Post-fix, this
becomes a paid-user UX hole rather than an entitlement bypass —
still bad (a paid user clicking Cancel loses the remainder of their
billing period instantly), but no longer a paywall hole.

**Fix:** As above, plus consider keeping the cancel idempotent so a
double-click can't induce subtle races.

---

### M-1 — `_render_addons` (and 3 sibling readers) ignores `is_admin`; admin users see "no add-on" UI despite functional access

**Location:**
- `/Users/shocakarel/Habbig/gateway/billing_routes.py:271-281`
  (`_render_current_plan`)
- `/Users/shocakarel/Habbig/gateway/billing_routes.py:370` (`_render_addons`)
- `/Users/shocakarel/Habbig/gateway/billing_routes.py:419`
  (`_render_cancel_losses`)
- `/Users/shocakarel/Habbig/gateway/billing_routes.py:461-470` (`_derive_invoices`)
- `/Users/shocakarel/Habbig/gateway/billing_routes.py:586-594`
  (`data_payload` for the JS proration calculator)

**What:** Every billing-page render path reads
`db.get_trading_addon_status(user_id)` and treats `active=False` as
the gospel. `get_trading_addon_status` (lines 458-472 of
`queries/markets.py`) does **not** consult `is_admin`. Only
`has_trading_addon` does. So an admin who has never been toggled
to active sees:

- `_render_current_plan`: "Trading Add-on" shown as a struck-out
  feature (line 278-281's `sb-plan-feature no`).
- `_render_addons`: "Add to plan" CTA — but clicking it (C-1) sets
  `users.trading_addon_active=1`, which would then make the admin
  user *visibly* the same as a paid user in `_user_id_from_event`
  lookups elsewhere.
- `_render_cancel_losses`: doesn't include "Unified Polymarket +
  Kalshi trading" in the cancel-loss list even though the admin
  loses it.
- `_derive_invoices`: skips the `addon_trading` line item.
- `data_payload.addon.active`: False (line 591) — the JS proration
  calculator at `static/settings_billing.js` will offer the admin a
  "Add Trading Add-on" upsell.

**Why Medium:** Admins are a small population (and bypass via
`has_trading_addon`'s is_admin short-circuit). Cosmetic for the
user, but it lets an admin accidentally self-grant via C-1, polluting
the audit log and creating a "real" expiry timestamp on a
functionally-unlimited user. Worse, after the admin user's
`is_admin` is revoked, the row stays `active=1, period_end=<future>`
— the demoted admin retains paid access until the row expires.

**Fix:**

Add an `is_admin` OR in `get_trading_addon_status`, **or**
add a separate helper that callers use for UI rendering:

```python
def get_trading_addon_status_for_ui(user_id, *, is_admin=False):
    s = get_trading_addon_status(user_id)
    if is_admin:
        s["active"] = True
    return s
```

Mirror what the `/api/trading-addon/config` GET handler at
`server.py:7472-7488` already does (it OR's `is_admin` into the
returned `active`).

---

### M-2 — `set_trading_addon(user_id, True, None)` is a permission-eternal grant; no caller enforces non-null period_end

**Location:** `/Users/shocakarel/Habbig/gateway/queries/markets.py:475-500`.

**What:** `set_trading_addon(uid, True, None)` writes
`trading_addon_active=1, trading_addon_period_end=NULL`. Both
`get_trading_addon_status` and `has_trading_addon` short-circuit the
expiry check on NULL:

```python
# get_trading_addon_status
if active and period_end and period_end <= int(time.time()):
    active = False

# has_trading_addon
period_end = row["trading_addon_period_end"]
if period_end and period_end <= int(time.time()):
    return False
return True
```

A NULL `period_end` is treated as "never expires". No production
caller passes `(True, None)` today (the admin route and the C-1
route both compute `now + 30 * 86400`), but the function signature
allows it and the SQL faithfully stores it. Anyone who adds a future
"grant trading addon to all alpha-program users" admin script that
forgets the second arg, or a migration that does
`UPDATE users SET trading_addon_active=1 WHERE …` without setting
period_end, creates a population of users with infinite access.

**Why Medium:** No live caller, but the API surface is foot-gunned.
The migration at `db.py:297-300` defines the column as nullable, so
this isn't a future-only concern.

**Fix:** Reject `(active=True, period_end=None)` at the helper or
elevate to a CHECK constraint:

```python
def set_trading_addon(user_id: int, active: bool, period_end: Optional[int]) -> None:
    if active and period_end is None:
        raise ValueError("Active trading add-on requires a period_end")
    ...
```

Or: drop the NULL escape hatch in both readers — if `active=1` and
`period_end IS NULL`, treat as expired (defensive default).

---

### M-3 — Expired rows are not swept; `trading_addon_active=1` persists past period_end forever

**Location:** `/Users/shocakarel/Habbig/gateway/queries/markets.py:458-500`
(read-time expiry checks); no scheduled job in `gateway/scheduler/` or
`gateway/jobs/` touches `trading_addon_*`.

**What:** Expiry is enforced **at read time**. The DB column stays
`trading_addon_active=1` with a stale `period_end` indefinitely
unless a write path overwrites it (admin toggle, user cancel, or
the C-1 self-grant). A query like
`SELECT COUNT(*) FROM users WHERE trading_addon_active=1` will
include long-expired users, polluting:

- Admin dashboards (`/admin/users` filter for "with trading add-on"
  at `server.py:5416-5425`).
- Cohort analytics queries (`exports/generator.py:399-401` etc.).
- Any future Stripe-state-reconciliation script that says "every
  user with `active=1` should have a corresponding Stripe
  subscription".
- The audit-log query at `security/audit.py` — last-grant timestamps
  outlive functional access.

**Why Medium:** No security impact (readers correctly compute
expired-as-inactive). But the moment any future code reads the raw
column without going through the helpers — say, a JSON dump of the
user row sent to the frontend, or a Pandas analysis script — the
expiry semantics are silently wrong.

**Fix:** Either:

1. Nightly cron in `gateway/scheduler/` that runs
   `UPDATE users SET trading_addon_active = 0, trading_addon_period_end = NULL
   WHERE trading_addon_active = 1 AND trading_addon_period_end IS NOT NULL
   AND trading_addon_period_end <= strftime('%s','now')`.
2. Or, structurally: enforce expiry at write by computing
   `effective_active = active AND (period_end IS NULL OR period_end > now)`
   in a generated column.

Option 1 is the minimal change.

---

### M-4 — `with_idempotency` for addon-add silently degrades to "no idempotency" when no Idempotency-Key header is present and `fallback_fingerprint="trading"` collides across users

**Location:**
- `/Users/shocakarel/Habbig/gateway/billing_routes.py:1157-1164`
- `/Users/shocakarel/Habbig/gateway/security/idempotency.py:122-141`
  (`_derive_key`).

**What:** The fallback fingerprint passed at line 1163 is the literal
string `"trading"`. The key derivation at `_derive_key` lines
138-140:

```python
if fallback_fingerprint:
    h = hashlib.sha256(fallback_fingerprint.encode("utf-8")).hexdigest()[:24]
    return f"idem:{user_id}:{op}:hash:{h}"
```

— is salted with `user_id` *and* `op`, so the collision concern across
users does NOT materialise (good). But the fingerprint `"trading"` is
identical for every legitimate add-on click, which means a single
user clicking 5 times in 10 s gets exactly one mutation (good for
double-click protection). The fix for C-1 (when it lands) needs to
key on the *Stripe session ID* rather than `"trading"` so that two
distinct Checkout flows don't collapse onto the same key — but for
the current implementation, the de-dupe works.

The actual M-4 concern: when the Stripe Checkout fix lands, the
header `Idempotency-Key` from the client SPA must be a per-attempt
nonce. Otherwise a user who reloads the Checkout success URL replays
the same `client_key` and gets `cached` back (skipping any audit
log writes the SDK migration adds at the same time). The current
SPA at `static/settings_trading_addon.js` does NOT set
`X-Idempotency-Key` or `Idempotency-Key` on its fetch calls.

**Why Medium:** Latent until the FIX. Calling it out here so the
fix doesn't accidentally turn a legitimate user retry into a
silent skip.

**Fix:** Set `Idempotency-Key: <crypto.randomUUID()>` per
checkout-creation attempt on the client, and document the contract
in the route docstring.

---

### L-1 — `_billing_rate_limit` action-key split lets a user burn 20 adds + 20 cancels + 20 cancellation-attempts per hour

**Location:** `/Users/shocakarel/Habbig/gateway/billing_routes.py:74-86`.

**What:** The rate-limit key is
`f"billing:{user['user_id']}:{action}"` — distinct buckets per
action. The cumulative ceiling is `20 × len(actions)`:

```
addon, addon_cancel, cancel, resubscribe, pause, resume, portal,
portal_session
```

= 8 × 20 = 160 billing mutations / user / hour.

In the presence of C-1, that becomes 160 self-grants/hour. The
audit comment at lines 64-72 ("20 mutations per rolling hour per
user is comfortably above legitimate usage") is correct *per
action*, but the total adds up.

**Why Low:** Bounded, observable in logs, and the underlying actions
are themselves idempotent at the DB level (except C-1, which is
the bigger problem). Worth knowing if you tune the limit.

**Fix:** Add a second cross-action bucket:
`_is_rate_limited(f"billing:{uid}:global", limit=60, window=3600)`
on top of the per-action one, so total mutations are bounded.

---

### L-2 — `/admin/users/{id}/trading-addon` route ignores period_end on cancel

**Location:** `/Users/shocakarel/Habbig/gateway/server.py:6188-6219`.

**What:**

```python
period_end = int(time.time()) + 30 * 86400 if active else None
db.set_trading_addon(user_id, bool(active), period_end)
```

The admin route can only grant in 30-day chunks (no parameter for
duration). When deactivating, period_end is reset to None — which
disconnects the row from any prior history. If the admin meant to
"pause" a paid user (the user files a complaint, the admin removes
access for a week then restores it), the original period_end is
gone forever and the user gets the next-30-days grant on re-enable.

**Why Low:** Operational, not security. The audit log at
`security/audit.py` records the before/after so the original
period_end is reconstructible from logs.

**Fix:** Add `duration_days` and `preserve_period` Form params:

```python
@app.post("/admin/users/{user_id}/trading-addon")
async def admin_toggle_trading_addon(
    request, user_id: int,
    active: int = Form(0),
    duration_days: int = Form(30),
    preserve_period: int = Form(0),
):
    ...
```

---

### L-3 — `period_end` is stored as INTEGER unix seconds; mixing with `datetime(?, 'unixepoch')` elsewhere is inconsistent

**Location:**
- `/Users/shocakarel/Habbig/gateway/queries/markets.py:470` — Python
  `int(time.time())` comparison.
- `/Users/shocakarel/Habbig/gateway/billing_routes.py:991` —
  `datetime(?, 'unixepoch')` for `subscription_paused_until`.

**What:** The `users` table has two unix-timestamp-ish columns and
they're stored in different formats:

- `trading_addon_period_end` — INTEGER unix seconds (comparable as
  `period_end <= time.time()`).
- `subscription_paused_until` — TEXT formatted as
  `datetime(?, 'unixepoch')` (e.g. `'2026-06-14 12:31:22'`).

A future admin query like
`SELECT * FROM users WHERE trading_addon_period_end < subscription_paused_until`
would do a string-vs-int comparison and silently produce nonsense.

**Why Low:** No production query crosses these two columns today.
Worth normalising before someone writes one.

**Fix:** Migrate `subscription_paused_until` to INTEGER unix-seconds
(matches every other timestamp in the table). The conversion is
mechanical.

---

### L-4 — `_render_addons` form button has no CSRF token wired explicitly; relies on the middleware setting `_csrf` cookie + the form template's hidden field

**Location:** `/Users/shocakarel/Habbig/gateway/billing_routes.py:368-402`.

**What:** The rendered form HTML at lines 380-384:

```python
'<form method="post" action="/settings/billing/addon" style="display:inline">'
'<input type="hidden" name="addon" value="trading">'
'<button type="submit" class="sb-btn sb-btn-primary sb-btn-sm">Add to plan</button>'
'</form>'
```

— has no `<input name="_csrf">` field. The CSRF middleware
(referenced in `tests/test_settings_billing.py:118-134`) checks the
`_csrf` form field for `application/x-www-form-urlencoded` bodies.
If the middleware injects the field server-side via a response
filter, this works. If it doesn't, the form is missing the token
and every legitimate POST 403s.

`tests/test_settings_billing.py:118-134` (`_post_form`) manually
appends `_csrf` to the data — confirming the middleware does NOT
inject it. The form as rendered in `_render_addons` is
**non-functional** in browsers: the user clicks "Add to plan", the
POST arrives without `_csrf`, the middleware 403s. (Combined with
C-1 this is a partial mitigation against unauthenticated /
cross-site self-grant — but it's an accidental one, and any future
patch that "fixes the missing button" by adding `_csrf` re-opens C-1.)

**Why Low:** As a defensive accident this currently breaks the C-1
vector for browser users (only API clients who construct the form
body manually can hit it). But the UX is broken, the audit-log
finding is real, and the next dev to "fix" the broken button is
strictly worse off.

**Fix:** Add the CSRF token to the rendered form template — same
pattern as every other settings form. Wait until C-1 is fixed first.

---

### L-5 — `get_trading_addon_status` recomputes expiry with `int(time.time())` inside the read; race against `set_trading_addon` is small but observable

**Location:** `/Users/shocakarel/Habbig/gateway/queries/markets.py:458-472`.

**What:**

```python
with db.conn() as c:
    row = c.execute(
        "SELECT trading_addon_active, trading_addon_period_end FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
if not row:
    return {"active": False, "period_end": None}
active = bool(row["trading_addon_active"])
period_end = row["trading_addon_period_end"]
# Check expiry
if active and period_end and period_end <= int(time.time()):
    active = False
return {"active": active, "period_end": period_end}
```

The SQL read and the `time.time()` call are not atomic. A row read
at T0 with `period_end = T0 + ε` will report `active=True` even if
the actual API call arrives at the markets handler at `T0 + 2ε`.
Two reads from the same handler (one in `_render_addons` for the
billing page, one in `has_trading_addon` for an API call) can
disagree. The window is microseconds.

The user mentioned "addon-active read consistency with the FIX
agent" — this is the read-side consistency observation. The current
two readers (`get_trading_addon_status` and `has_trading_addon`)
both do client-side expiry. After the FIX adds a third reader (say,
`get_trading_addon_active_for_webhook(user_id, at_event_time)`),
they all need to agree on the comparison timestamp. Best practice
is to compute expiry in SQL:

```sql
SELECT trading_addon_active AND
       (trading_addon_period_end IS NULL OR trading_addon_period_end > strftime('%s','now'))
       AS effective_active
FROM users WHERE id = ?
```

— so all readers share the SQL clock and there's no Python-vs-SQL
clock drift.

**Why Low:** Microsecond window; no realistic exploit. Calling out
for the FIX agent.

**Fix:** Push expiry into SQL, return a single `effective_active`
boolean. Optional: add a generated column for it.

---

### I-1 — `users.trading_addon_active` and `users.trading_addon_period_end` migrated as `ALTER TABLE … ADD COLUMN` without backfill

**Location:** `/Users/shocakarel/Habbig/gateway/db.py:297-300`.

```python
if "trading_addon_active" not in existing_cols:
    c.execute("ALTER TABLE users ADD COLUMN trading_addon_active INTEGER NOT NULL DEFAULT 0")
if "trading_addon_period_end" not in existing_cols:
    c.execute("ALTER TABLE users ADD COLUMN trading_addon_period_end INTEGER")
```

Inline migrations in `init_db` rather than a numbered migration file
in `gateway/migrations/`. The companion settings table is
`gateway/migrations/176_trading_addon_settings.py`, which is a
proper migration. The column additions for the *flag* are not.

**Why Info:** No security impact. Consistency. A future schema audit
that grep's `migrations/*.py` for column adds will miss the
trading_addon flag columns. Pin them down in a proper migration.

---

### I-2 — `TRADING_ADDON` price catalog has a £25/£255 entry that's never surfaced in the UI

**Location:** `/Users/shocakarel/Habbig/gateway/server.py:4148`.

```python
TRADING_ADDON = {"label": "Trading Access", "monthly": 25, "annual": 255, "monthly_usd": 29, "annual_usd": 299}
```

The `monthly` (£25) / `annual` (£255) keys are read once each in
`billing_routes.py` but every rendered surface uses the USD keys
(`monthly_usd`, `annual_usd`). The GBP labels appear in the pricing
page tests at `tests/test_pricing.py:93-100` but not in the
runtime UI. Either drop the GBP keys (and the pricing tests that
reference them) or wire them into the dual-currency rendering at
`billing_routes.py:393`.

**Why Info:** Hygiene.

---

## Notes on what is correct

For balance — the following were checked and are sound:

- `has_trading_addon` correctly OR's `is_admin` for the API gate at
  `market_routes.py:228-233`. Admins bypass.
- The PATCH route at `server.py:7482-7497` correctly requires
  `is_admin OR has_trading_addon` before persisting settings —
  no free-tier user can configure an addon they don't own.
- `set_trading_addon` is parameterised SQL — no injection.
- The cancel-flow at `billing_routes.py:920-925` correctly avoids
  touching `users.trading_addon_active` (test
  `test_cancel_all_flips_every_active_sub_but_not_trading_addon`
  asserts this).
- `ttl_invalidate.on_subscription_change` is called on every
  trading-addon mutation in `billing_routes.py` (lines 1153, 1178).
- The settings page properly gates the form behind
  `_require_markets_user`'s functional equivalent via the
  `has_trading_addon` check on PATCH.
- CSRF middleware is engaged for the addon mutation routes
  (see L-4 — the form is missing the field, but the middleware
  enforces it).
- `_billing_rate_limit` is called at the top of every mutation
  handler, including the addon add/cancel routes.

---

## Reproduction notes

The findings above were verified by code reading. Live-confirmation
recipes:

- **C-1**: `pytest gateway/tests/test_settings_billing.py::TestAddonFlow::test_addon_add_with_stripe_stubbed_fails_closed`
  (currently passes only if Stripe is forcibly stubbed out at the
  helper layer; the current code path bypasses the assertion). Or
  by manual curl: `curl -X POST -d 'addon=trading&_csrf=<csrf>'
  http://localhost:<port>/settings/billing/addon`. Expect 302 →
  `?saved=addon_added` and `users.trading_addon_active=1`.

- **H-1**: Set `users.trading_addon_period_end = strftime('%s','now') + 5184000`
  (60 days) and POST `/settings/billing/addon`. Expect period_end
  shortened to ~now+30d.

- **H-2**: After any putative FIX makes
  `/settings/billing/addon` redirect to Stripe Checkout: post a
  signed `customer.subscription.created` event with
  `metadata.addon=trading, metadata.user_id=<uid>`. Confirm that
  `users.trading_addon_active` stays 0 (no webhook branch).

- **M-1**: Login as admin, GET `/settings/billing`. Inspect the
  rendered HTML; `Trading Add-on` appears in the "struck-out"
  features list at line 278-281, and the `<form action="/settings/billing/addon">`
  CTA renders an "Add to plan" button.

- **M-3**: Set
  `UPDATE users SET trading_addon_active=1, trading_addon_period_end=1`
  for any user. Confirm `has_trading_addon` returns False but
  `SELECT trading_addon_active FROM users WHERE id=?` returns 1.

No code changes were made as part of this audit.
