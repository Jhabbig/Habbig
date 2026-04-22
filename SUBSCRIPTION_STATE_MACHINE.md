# Subscription state machine

Last updated: 2026-04-22

Covers the full lifecycle of a narve.ai subscription — from signup
through lapse, refund, gift, and reactivation. The authoritative data
structures are:

* `users.subscription_tier` — the main-product tier (`"none"`, `"pro"`,
  `"enterprise"`).
* `users.subproduct_subscriptions` — JSON blob per sub-brand:
  `{slug: {status, period_end, stripe_sub_id}}`. See
  [migration 060](gateway/migrations/060_subproduct_subscriptions.py).
* `gifted_subscriptions` — gift-sender vs gift-recipient relationship.
* `processed_stripe_events` — webhook idempotency ledger; see
  [migration 061](gateway/migrations/061_processed_stripe_events.py).

The Stripe-webhook entry point is hardened in
[`stripe_webhook_hardening.py`](gateway/stripe_webhook_hardening.py).
Nightly reconciliation lives in
[`jobs/reconcile_subscriptions.py`](gateway/jobs/reconcile_subscriptions.py)
and trusts Stripe as the source of truth when webhooks drift.

---

## States

Every (user, subproduct) pair sits in exactly one of these:

| State | `status` value | Access | Notes |
|---|---|---|---|
| **NEW** | (no row) | None | User exists but has never held this subproduct |
| **ACTIVE** | `active` | Full | Stripe subscription in `active` / `trialing` |
| **PAST_DUE** | `past_due` | Full, flash warning | Stripe retrying payment — user keeps access while retrying |
| **CANCELLING** | `active` + `cancel_at_period_end=true` | Full until `period_end` | User hit "Cancel" — access persists until period rollover |
| **GRACE** | `canceled`, `period_end` > now | Full | Stripe cancelled but paid period hasn't elapsed; kept so refund-and-resub don't lose history |
| **LAPSED** | `canceled`, `period_end` ≤ now | None | Period expired — webhook hardening revokes sessions + embed widgets |
| **PAUSED** | `paused` | None | Stripe native pause (rare, admin-only today) |

Tier transitions (main product) mirror the same set but live on
`subscription_tier` instead of the JSON blob.

---

## Events

| Event | Source | Transition |
|---|---|---|
| `checkout.session.completed` | Stripe webhook | NEW → ACTIVE |
| `customer.subscription.updated` with `cancel_at_period_end=true` | Stripe webhook | ACTIVE → CANCELLING |
| `customer.subscription.updated` with `cancel_at_period_end=false` | Stripe webhook | CANCELLING → ACTIVE (undo-cancel) |
| `customer.subscription.deleted` | Stripe webhook | CANCELLING / ACTIVE → GRACE (if `period_end` future) or LAPSED (else) |
| `invoice.payment_failed` | Stripe webhook | ACTIVE → PAST_DUE |
| `invoice.payment_succeeded` after PAST_DUE | Stripe webhook | PAST_DUE → ACTIVE |
| Clock: `period_end` reached in GRACE | Nightly reconcile | GRACE → LAPSED |
| Manual refund via Stripe dashboard | Stripe webhook (`charge.refunded`) | No automatic status change — operators use the admin tool to force LAPSED if needed |
| Gift accepted | `POST /api/gifts/accept` | NEW → ACTIVE (gift-backed) |
| Gift sender cancels their own sub | Stripe webhook | Gift recipient untouched — separate Stripe subscription |
| Subproduct downgrade (e.g. Pro → Trader add-on only) | `POST /api/billing/downgrade` | ACTIVE → CANCELLING for the higher tier; lower tier ACTIVE immediately (no proration today) |
| Chargeback | Stripe webhook (`charge.dispute.created`) | Stripe pauses; our handler logs but does NOT immediately lock out — fraud team reviews first |

---

## Side effects on state transition

| Transition | Sessions | Embed widgets | Email | Access cache | Cached feeds |
|---|---|---|---|---|---|
| → GRACE | kept | kept | subscription_cancelled | invalidated | invalidated (`ttl_invalidate.on_subscription_change`) |
| → LAPSED | **revoked** | **deactivated** | lapse_notice | invalidated | invalidated |
| → ACTIVE (from PAST_DUE) | kept | re-activated | payment_recovered | invalidated | invalidated |
| → ACTIVE (resub after LAPSED) | new login required | kept deactivated until user edits | welcome_back | invalidated | invalidated |

Session revocation is defence-in-depth — the access gate
(`subproduct_access.require_subproduct_access`) also re-checks the
blob on every request, so even a non-revoked session stops serving
gated endpoints the moment the status flips.

---

## Edge cases

### Webhook replay / duplicate delivery

The `processed_stripe_events` row is written before the handler runs
the per-event branches. Second delivery with the same `event_id`
short-circuits with `{"status": "already_processed"}` and a 200.
Stripe does not retry 2xx responses.

### Mode mismatch (test vs live)

`reject_mode_mismatch(event)` returns 400 when a `livemode=False`
event hits production (or vice versa). Prevents a stray test-mode
webhook from flipping real subscription state.

### Mid-cycle downgrade Pro → Trader

Current behaviour (2026-04-22): the higher-tier subscription enters
CANCELLING and the lower-tier subscription starts ACTIVE immediately.
The user has **both** until `period_end` of the cancelling tier, then
only the lower tier. No proration.

Flagged for follow-up — the business wants proration to match Stripe's
default, but the implementation requires either (a) pro-rating at
checkout via Stripe's `proration_behavior=create_prorations` option
or (b) custom credit handling. Not landing today.

### Gift sender cancels own sub, recipient unaffected

Gifts are modelled as separate Stripe subscriptions with the sender
as the payer and a `metadata.gift_recipient_user_id` on the sub.
When the sender cancels their *personal* subscription, Stripe does
not touch the gift — they're distinct Stripe records. Verified in
`gifted_subscriptions` schema (revoke requires an explicit admin
action on that row).

### Refund via Stripe dashboard

Refunds fire `charge.refunded`, not a subscription event. The default
behaviour is to leave the user in their current state — operators
use the admin tool (`POST /admin/api/users/{id}/force-lapse`) to
push them into LAPSED if the refund should also yank access.
Deliberately manual: most refunds are customer-service gestures that
shouldn't change access.

### User at Pro cancels → grace → subscribes again

1. User at ACTIVE clicks Cancel → Stripe marks
   `cancel_at_period_end=true` → state CANCELLING. Access persists.
2. `period_end` reached. Stripe fires `customer.subscription.deleted`.
3. Our handler: GRACE → LAPSED. Sessions revoked, widgets off.
4. User clicks "Resubscribe". Stripe Checkout → new subscription
   created → `customer.subscription.created` → state ACTIVE.
5. New session required (step 3 revoked the old one).

No data loss. `gifted_subscriptions` history preserved. The
subproduct JSON is overwritten with the new sub's details; the old
Stripe ID is kept in the webhook ledger for audit.

### User deletes account mid-cycle

The scheduled deletion job (`process_scheduled_deletions`) runs 30
days after the user hits "Delete". During that window the user can
cancel the deletion. If they don't, we:

1. Cancel the Stripe subscription.
2. Anonymise their row (email → `deleted_{id}@deleted.narve.ai`).
3. Cascade personal data — sessions, notifications, saved predictions,
   intelligence conversations.
4. Retain financial + research records (legally required).

Sub state at hard-delete becomes irrelevant — the anonymised user row
is functionally inert.

---

## Reconciliation drift detection

The nightly `reconcile_subscriptions` job:

* Queries every user with a `stripe_sub_id` in their subproduct blob.
* For each, calls `stripe.Subscription.retrieve` and compares
  `status` / `current_period_end`.
* Writes the Stripe values back to our blob when they disagree.
* Invalidates cached feeds for that user
  (`ttl_invalidate.on_subscription_change(user_id)`).
* Alerts admin email if > 5% of checked users drifted — a sign the
  webhook handler dropped events recently.

Runs at 03:17 UTC daily (off-peak for Stripe's rate limits and our
cron schedule).

---

## Acceptance tests

Stripe hardening itself is covered by
[`tests/test_stripe_webhook_hardening.py`](gateway/tests/test_stripe_webhook_hardening.py):

* `test_first_event_not_already_processed`
* `test_second_event_already_processed` — duplicate delivery
* `test_mode_mismatch_in_production` — livemode enforcement
* `test_mode_match_in_production`
* `test_mark_processed_stamps_row` — ledger completes cleanly
* `test_mark_processed_records_error` — partial failure captured

The scenarios in the **Edge cases** section above are enforced at the
business-logic level; each has a named transition and is documented so
reviewers have a single page to check against when Stripe changes a
webhook shape or we ship a new tier.
