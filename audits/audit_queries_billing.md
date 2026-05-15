# Adversarial Audit — `gateway/queries/billing.py`

Date: 2026-05-15
Auditor: Claude (Opus 4.7, 1M context)
Repo tip at scan start: `61180ce` on branch `feature/platform-build`

---

## 0. Target-file resolution (READ FIRST)

The brief names `gateway/queries/billing.py`. **That file does not exist in the
repo.** A full search for `queries/bill*` returned no matches:

```
find /Users/shocakarel/Habbig/gateway -maxdepth 4 -type f -iname "*billing*"
  → /Users/shocakarel/Habbig/gateway/billing_routes.py
  → /Users/shocakarel/Habbig/gateway/tests/test_billing_portal.py
  → /Users/shocakarel/Habbig/gateway/tests/test_settings_billing.py
  → static/* (HTML/CSS/JS only)
```

The billing/subscription state-mutation logic that *would* live in a
`queries/billing.py` is split across these files instead:

| Role | Path | LOC |
|------|------|-----|
| Queries layer for subs domain | `gateway/queries/subscriptions.py` | 585 |
| Trading add-on queries (period-end fields) | `gateway/queries/markets.py:458-605` | (≈150 in scope) |
| Web/UI mutation handlers | `gateway/billing_routes.py` | 1263 |
| Stripe webhook dispatch | `gateway/stripe_webhook_routes.py` | 308 |
| Stripe hardening / state mutators | `gateway/stripe_webhook_hardening.py` | 441 |
| Nightly reconcile | `gateway/jobs/reconcile_subscriptions.py` | 179 |
| Subscriptions schema | `gateway/db.py:46-58` (SQL) | 13 |

This audit treats those six files as the de-facto `queries/billing.py` surface
and applies the five attacker classes from the brief to them. If a finding
points at a non-`queries/` file, the audit attributes it to the file where
the bug actually lives and notes how it would migrate if the code were ever
extracted into the named module.

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 2 |
| Medium   | 5 |
| Low      | 4 |
| Info     | 3 |
| **Total**| **14** |

## Top 3 findings (ranked by exploitability × impact)

1. **HIGH-1** — Subscription state mutations in
   `billing_routes.py` cancel/resubscribe/pause and in `stripe_webhook_routes.py`
   `_grant_access` / `_update_plan` / `_record_payment` run multi-statement
   sequences (subs UPDATE → users UPDATE → cache invalidate → idempotency stamp)
   on the auto-commit `db.conn()` context manager. The `@contextmanager`
   commits only on exit; if a step in the middle raises, the prior writes are
   already in the WAL but unflushed by *that* context, and the next
   handler invocation re-reads a half-baked state. Examples: pause handler
   inserts `subscription_pauses` then updates `users.subscription_paused_until`
   in the same `with db.conn()` block — atomic — but the *finalize_cancel_attempt
   plus the cache invalidate plus the email enqueue* happen OUTSIDE that block,
   in three further independent connections. A crash between (1) flipping
   subs to `cancelled` and (3) queueing win-back emails leaves the user
   cancelled-with-no-emails on retry (idempotency key absorbs the duplicate
   cancel). Worse, the `apply_subscription_cancelled` webhook handler walks
   three tables (`subproduct_subscriptions` JSON → sessions revoke → embed
   widgets deactivate) across two `db.conn()` blocks AND a `mark_processed`
   block, with no transaction wrapping. If the second block fails the user is
   cancelled in JSON but their sessions stay live and embeds stay published.
   (See HIGH-1.)

2. **HIGH-2** — `_user_id_from_event` (`stripe_webhook_hardening.py:241-269`)
   trusts `event.data.object.metadata.user_id` as the authoritative narve
   user_id with no ownership check against `users.stripe_customer_id`.
   `_grant_access` (`stripe_webhook_routes.py:84-123`) consumes that same
   metadata and writes a `subscriptions` row with the claimed `user_id`,
   `dashboard_key`, and `stripe_sub_id`. The signature check at the route
   stops *forged* events, but **any actor who can edit subscription metadata
   in your live Stripe account** (legitimate staff with Stripe dashboard
   access, a compromised Stripe API key, or any system that calls
   `stripe.Subscription.create(...)` server-side) can mint a subscription
   for arbitrary `user_id` + `dashboard_key`. There is no cross-check that
   the Stripe `customer` on the subscription is the *same* customer narve
   has recorded against that `user_id`. The `_record_payment` and
   `_update_plan` paths inherit the same trust. Customer-id ownership is
   enforced exactly once in the codebase — in `api_billing_portal_session`
   (`billing_routes.py:1190-1210`) where it just looks up the *own* user's
   customer-id to seed the portal session. No webhook path verifies that
   the customer on the inbound event belongs to the metadata's user. (See
   HIGH-2.)

3. **MED-1** — `period_end` vs `current_period_end` vs `expires_at` drift
   across three storage shapes that don't share a single source of truth.
   The `subscriptions` table has `expires_at` (unix seconds, nullable);
   `users.subproduct_subscriptions` is a JSON blob where each entry has a
   `period_end` field (set by the reconcile job from Stripe's
   `current_period_end`); `users.trading_addon_period_end` is its own
   unix-second column; `users.intelligence_addon_period_end` is yet another.
   The cancel handler (`billing_routes.py:887-957`) flips
   `subscriptions.status = 'cancelled'` but never touches `expires_at`, so
   `has_active_subscription` still grants access until `expires_at` passes —
   intentional ("keeps access until period end"), but `_grant_access` /
   `_update_plan` / `_record_payment` (the three Stripe write paths) **never
   write `expires_at` at all**. The reconcile job only updates the JSON
   blob's `period_end`, not the `subscriptions.expires_at`. Result: the
   "real" period end lives in three places that can disagree, and
   `has_active_subscription` reads from the one (`subscriptions.expires_at`)
   that no Stripe path updates. (See MED-1.)

---

## Findings

### HIGH-1 — Multi-statement billing state changes are not atomic

**Locations:**
- `billing_routes.py:887-957` (`settings_billing_cancel` step=3)
- `billing_routes.py:959-1005` (`settings_billing_pause`)
- `billing_routes.py:1007-1026` (`settings_billing_resume`)
- `billing_routes.py:1029-1046` (`settings_billing_resubscribe`)
- `stripe_webhook_hardening.py:272-361` (`apply_subscription_cancelled`)
- `db.py:257-266` (the `conn()` context manager)

**What:** `db.conn()` is a `@contextmanager` that opens a fresh
`sqlite3.connect`, yields, then **commits on normal exit and closes in a
finally**. SQLite is already in WAL mode (see DB_HEALTH.md) so each
connection auto-`BEGIN`s on the first write. The problem is that several
billing flows do **multiple `db.conn()` blocks** in sequence, with non-DB
side effects between them, and none of them open an explicit `BEGIN
IMMEDIATE` or share a single connection across the sequence:

`settings_billing_cancel` (step=3):

1. `with db.conn() as c: UPDATE subscriptions SET status='cancelled' …`
   (committed when `with` exits)
2. `_finalize_cancel_attempt(...)` opens its **own** `db.conn()` and
   `UPDATE cancellation_attempts SET outcome='cancelled', …` (separate
   transaction).
3. `_queue_winback_emails(...)` enqueues two async jobs to the email queue
   (network/queue side effect, no DB rollback semantics).
4. `ttl_invalidate.on_subscription_change(...)` flushes per-user feed cache
   keys (in-process, no rollback).

If step 2 raises (e.g. `cancellation_attempts` row was already deleted by a
double-submit), the user is **already cancelled** but the attempt row never
gets `outcome='cancelled'` and the win-back emails never enqueue. The
`with_idempotency` wrapper at step (0) (`billing_routes.py:917-947`)
**collapses retries within 30 s**, so the cron-driven retry that would normally
heal this just returns the cached "cancelled: True" response without re-running.

`apply_subscription_cancelled` (webhook):

1. `with db.conn(): …_update_subproduct_status` — writes the JSON blob
   on `users.subproduct_subscriptions` (commits).
2. Same wrapper opens a **second** `with db.conn()` block that walks three
   session-table names (`narve_sessions`, `sessions`, `user_sessions`) and
   then updates `embed_widgets` — committed at block exit.
3. Outside both blocks: `invalidate_user` (cache), `enqueue_email`.

If step 2 partially fails (one of the three session-table UPDATEs raises and
the rest of the try/except blocks swallow), the user has been cancelled in
the JSON blob but their live session cookies are still valid and their
public embed widgets are still rendering. The webhook returns 200 either
way; Stripe won't retry; manual reconciliation (`reconcile_subscriptions`)
only fixes JSON `status`/`period_end`, not session/embed state.

`settings_billing_pause`:

1. `with db.conn() as c:` — INSERT INTO subscription_pauses **and** UPDATE
   users.subscription_paused_until in the same block. **Atomic.** Good.
2. `_finalize_cancel_attempt(...)` — separate connection, separate
   transaction.
3. cache invalidate.

If 2 raises, the user is paused but the attempt row is stuck at the
penultimate step. The pause itself is intact; the analytics row is wrong.
Lower impact than cancel, but still drift.

**Why this matters:** SQLite is single-writer per connection but the
contextmanager pattern at `db.py:258` opens a fresh connection every call.
That's normally fine, but for a *sequence* of writes that the rest of the
app reads as one logical transition ("user has cancelled their
subscription"), the only correct boundary is one `BEGIN … COMMIT` across
everything that must succeed together. The current code spreads it across
2–4 transactions per handler.

**Attack/operational impact:**
- A flaky network drop during webhook delivery, or any uncaught exception
  inside `apply_subscription_cancelled`, leaves the cancel half-applied with
  no compensating retry — Stripe sees a 200 response.
- A user can race their own cancel: hit Cancel (step 3), then before the
  win-back emails enqueue, hit Resubscribe (`/settings/billing/resubscribe`,
  which flips status='active' for any cancelled-still-in-window row). The
  idempotency wrapper has a 30 s window; Resubscribe has no idempotency
  guard at all (`billing_routes.py:1029-1046`). Net effect: user is active
  again, attempt row reads `outcome='cancelled'`, win-back emails arrive
  7/30 days later anyway. Spammy but not destructive.

**Recommended fix:**
- Either: refactor each billing transition to a single `with db.conn() as c`
  block (best — pass `c` to helpers as a parameter rather than reopening).
- Or: explicit `c.execute("BEGIN IMMEDIATE")` + `c.commit()` at the top
  level of each handler, accept ALL inner helpers must take an injected
  cursor, and treat email-enqueue as a deliberate post-commit side effect
  inside a `try:` that logs but does not raise.
- Webhook path specifically: gather all DB mutations into one block, leave
  network/cache/email side effects strictly after the commit, and add a
  `processed_with_warning` flag to `processed_stripe_events` for partial
  failures so the reconcile job can re-drive them.

---

### HIGH-2 — No customer-id ownership cross-check on Stripe webhook writes

**Locations:**
- `stripe_webhook_routes.py:84-123` (`_grant_access`)
- `stripe_webhook_routes.py:126-163` (`_update_plan`)
- `stripe_webhook_routes.py:166-186` (`_record_payment`)
- `stripe_webhook_hardening.py:241-269` (`_user_id_from_event`)

**What:** `_grant_access` reads `event["data"]["object"]["metadata"]["user_id"]`
(or alias `narve_user_id`), coerces to int, and INSERTs/UPSERTs a
`subscriptions` row with that user_id. **There is no check that the
`event["data"]["object"]["customer"]` value matches `users.stripe_customer_id`
for that user_id.** Same pattern in `_update_plan` (uses metadata user_id
to find the subscriptions row to mutate) and `_user_id_from_event` (uses
metadata first, only falls back to `customer → users` lookup if metadata
is absent).

The defence relies entirely on Stripe's webhook signature check. That is
correct against forged HTTP — `stripe.Webhook.construct_event` plus the IP
allowlist make external forgery infeasible. **But it doesn't defend against
events that are genuinely signed by Stripe yet carry attacker-controlled
metadata.** Realistic threat vectors:

1. **Compromised Stripe API key (`STRIPE_SECRET_KEY`).** Any actor with
   read/write on the live Stripe account can call
   `stripe.Subscription.create(customer="cus_attacker", metadata={"user_id":
   "1", "dashboard_key": "intelligence-addon"})` and Stripe will dispatch
   a `customer.subscription.created` event with that metadata. The webhook
   handler will write a row granting user 1 the intelligence add-on.
   `STRIPE_SECRET_KEY` is rotated infrequently and lives in the same
   environment as `STRIPE_WEBHOOK_SECRET`; if one leaks they probably both
   leak, but only the webhook secret is consulted by the verification path.
2. **Malicious or careless Stripe-dashboard user.** Any human with edit
   access to the Stripe dashboard can paste arbitrary metadata onto an
   existing subscription. The
   resulting `customer.subscription.updated` event will be honoured.
3. **Multi-tenant cross-grant.** Two narve users (A, B) each have a real
   Stripe customer. If A's Stripe customer is somehow associated with a
   subscription whose metadata says `user_id=B`, B gets A's plan written
   onto their account. This is the standard cross-tenant IDOR shape ported
   to Stripe-as-source-of-truth.

The fallback in `_user_id_from_event` *does* a `customer → users` lookup,
but only when metadata is missing — and `_grant_access`/`_update_plan` use
metadata directly, not via this helper. Even when the helper IS used
(`apply_subscription_cancelled`, `apply_invoice_payment_failed`), it
trusts metadata first and only falls back to the customer lookup if
metadata is absent. There is no path that says "metadata says user X,
customer says Y, those must match Y == users.stripe_customer_id for X".

**Attack scope:** anyone with Stripe-dashboard edit or a leaked
`STRIPE_SECRET_KEY` can grant any plan to any user_id. Cleanup requires
manual `subscriptions` row deletion plus session revocation since the gate
caches access decisions.

**Recommended fix:**
- In every webhook write path, BEFORE the INSERT/UPDATE:
  ```python
  customer = obj.get("customer")
  if customer:
      with db.conn() as c:
          row = c.execute(
              "SELECT id FROM users WHERE id = ? AND stripe_customer_id = ?",
              (user_id, customer),
          ).fetchone()
      if not row:
          log.warning("customer mismatch: meta_user=%s customer=%s id=%s",
                      user_id, customer, event.get("id"))
          return  # treat as ignored, mark_processed will stamp it
  ```
- For checkout-session events that *establish* the link, write
  `users.stripe_customer_id` atomically with the first `subscriptions` row
  rather than relying on a separate code path to set it.
- Add a unit test that feeds a signed event with mismatched metadata into
  the dispatch and asserts the row is NOT written.

---

### MED-1 — Period-end timestamp drift across three storage shapes

**Locations:**
- `db.py:46-58` (subscriptions.expires_at)
- `db.py:299-300, 345-346` (users.{trading,intelligence}_addon_period_end)
- `queries/subscriptions.py:38-44` (`has_active_subscription` reads `subscriptions.expires_at`)
- `stripe_webhook_routes.py:108-121` (`_grant_access` does NOT write `expires_at`)
- `stripe_webhook_routes.py:147-161` (`_update_plan` does NOT write `expires_at`)
- `jobs/reconcile_subscriptions.py:107-110` (writes `period_end` into JSON blob, not into `subscriptions.expires_at`)
- `stripe_webhook_hardening.py:393-426` (`_update_subproduct_status` writes JSON blob `status` only, never `period_end`)

**What:** There are at least four places a "this sub ends at T" timestamp
is stored:

1. `subscriptions.expires_at` — unix-seconds INTEGER, nullable. Set by
   `upsert_subscription(duration_days=N)` (placeholder path) only. Read by
   `has_active_subscription`, `count_active_subscribers`,
   `get_user_active_subproducts`, `get_active_subscription_counts_by_dashboard`,
   `get_revenue_stats`, `get_user_primary_subscription`,
   `has_any_active_subscription` — **all the access-gate queries.**
2. `users.subproduct_subscriptions` JSON blob, key `period_end` — set by
   `reconcile_subscriptions` from Stripe's `current_period_end`. Read by
   the `subproduct_access` module (not in this audit's tree but referenced
   from `apply_subscription_cancelled`).
3. `users.trading_addon_period_end` — unix-seconds. Set by
   `set_trading_addon(user_id, active, period_end)` and read by
   `has_trading_addon`. Independent column.
4. `users.intelligence_addon_period_end` — unix-seconds, separate column,
   separate code path (`set_user_intelligence_addon` /
   `get_user_intelligence_addon_active`).

The Stripe webhook write paths (`_grant_access`, `_update_plan`,
`_record_payment`) **never write `subscriptions.expires_at`**. Inspect
`_grant_access`:
```python
"INSERT INTO subscriptions "
"(user_id, dashboard_key, plan, status, started_at, "
" stripe_sub_id, source) "
"VALUES (?, ?, ?, 'active', ?, ?, 'stripe') "
"ON CONFLICT(user_id, dashboard_key) DO UPDATE SET "
"  plan = excluded.plan, "
"  status = 'active', "
"  stripe_sub_id = excluded.stripe_sub_id, "
"  source = 'stripe'"
```

No `expires_at` column appears in the column list and the DO UPDATE
clause doesn't touch it either. SQLite defaults the column to NULL on
insert; the `has_active_subscription` query then treats NULL as "no expiry,
never ends":

```sql
WHERE user_id = ? AND dashboard_key = ? AND status = 'active'
  AND (expires_at IS NULL OR expires_at > ?)
```

**Consequence:** every Stripe-sourced subscription row in the
`subscriptions` table grants *perpetual* access until somebody flips
`status` away from `active`. If `customer.subscription.deleted` is missed
(network drop, malformed event, the partial-failure path from HIGH-1),
the row stays `active` forever because there's no expiry safety net.
The JSON blob's `period_end` (which IS kept fresh by the nightly
reconcile) is read by a different module entirely (`subproduct_access`)
— so the two layers can give opposite answers for the same user.

A second flavour of the same bug: the cancel handler at
`billing_routes.py:920-925` flips `status = 'cancelled'` but never
touches `expires_at`. The UI claims "you keep access until the end of
the billing period." That promise is implemented by reading
`pinfo["expires_at"]` in `_render_current_plan` — which is set by
`_user_plan_info` in server.py (outside this audit's tree). For
Stripe-sourced subs that ride NULL `expires_at`, the user keeps access
*forever* after cancelling locally. They only lose access if the
webhook reaches `_update_plan` with `status='canceled'` (note Stripe
spelling) and the local UPDATE flips status to `inactive`.

The `_record_payment` path is also blind to expiry — it only flips
`past_due → active` on `invoice.paid` events, never writes the next
period's `expires_at`.

**Attack:** open a subscription, then block the webhook from your
end (e.g. user-side IP-level filtering on the response, or just luck
with Stripe's delivery). The local `subscriptions` row stays
`status='active' expires_at=NULL` perpetually — even after the Stripe
sub is `cancelled` in Stripe's records. Re-running
`reconcile_subscriptions` won't fix it because that job only patches
the JSON blob, never the `subscriptions` table.

**Recommended fix:**
- Add `expires_at` to both branches of the upsert in `_grant_access`
  (read `current_period_end` off the Stripe event) and to the SET clause
  in `_update_plan`. Same for the cancel handler — set
  `expires_at = MIN(expires_at, cancel_at)` so the access gate sees the
  end-of-period.
- Make the reconcile job mirror Stripe's `current_period_end` into BOTH
  the JSON blob AND `subscriptions.expires_at` for any row with a
  matching `stripe_sub_id`. (Strictly: dedupe both writes into one
  cross-store helper.)
- Add a nightly safety job: any `subscriptions` row with
  `status='active'`, `source='stripe'`, and `expires_at IS NULL` is
  flagged for manual reconciliation — a sentinel for missed webhooks.

---

### MED-2 — Resubscribe has no idempotency, no rate-limit on the success path

**Location:** `billing_routes.py:1029-1046` (`settings_billing_resubscribe`)

**What:** The handler is wrapped by `_billing_rate_limit(user, "resubscribe")`
(per-user 20/hour) but does **not** go through `with_idempotency`. The
SQL is:

```sql
UPDATE subscriptions SET status = 'active'
WHERE user_id = ? AND status = 'cancelled'
  AND (expires_at IS NULL OR expires_at > ?)
```

This is naturally idempotent for the database (re-running can't double-
activate). However, every call ALSO does
`ttl_invalidate.on_subscription_change(user_id)` — which on each call
walks the per-user feed cache + every tier-scoped best-bets cache page
and invalidates them. A determined caller can hammer resubscribe 20×/hour
to repeatedly evict cached best-bets pages, costing the cache layer
re-computation. Not catastrophic (20/hour is bounded) but worth noting.

Pairs with HIGH-1 (resubscribe + cancel race window).

**Recommended fix:** wrap in `with_idempotency` keyed on
`billing_resubscribe` with TTL ~10 s. Cheaply prevents the
double-submit-induced cache thrash.

---

### MED-3 — Currency stored but never converted; price assumed USD everywhere

**Locations:**
- `queries/subscriptions.py:155-178` (`get_mrr_by_dashboard`)
- `billing_routes.py:96-138` (`PLAN_CATALOG_USD`)
- `static/pricing.html:32-37, 86-116` (advertises `priceCurrency: GBP`, prices in £)
- `static/settings_trading_addon.js:154-159` (user can pick GBP for `daily_cap`)
- `queries/markets.py:516, 551` (`daily_cap_currency` default 'USD', stored verbatim)

**What:** The product advertises GBP prices on `/pricing` (the public landing
schema.org `priceCurrency: "GBP"` and the visible "£15.00/mo" rows). The
plan catalog inside `billing_routes.py` is named `PLAN_CATALOG_USD` and the
MRR calculation in `queries/subscriptions.py:176` does:

```python
price_cents = int(round(float(cfg.get("price_usd") or 0.0) * 100))
out[dk] = active * price_cents
```

`get_mrr_by_dashboard` therefore returns "USD cents" by name but the
upstream catalog has stored what the public landing calls GBP. There is
no conversion at the read site. The admin dashboards that consume MRR
(`/admin/revenue`, `/admin/subproducts`) will render the same number with
a `$` or `£` glyph determined by the *template*, not by the data — i.e.
the number is currency-ambiguous and could be off by a factor of 1.27
(today's GBP→USD spot) depending on which side of the system the catalog
was last edited from.

Similarly `daily_cap_currency` is round-tripped through the trading-addon
settings ("USD" or "GBP") but no helper converts the stored `daily_cap`
into the user's chosen currency for comparison against any actual fill —
the field is purely cosmetic at the queries layer. Risk concentrates in
downstream consumers, but the queries-layer contract is unclear about
units.

**Attack/operational impact:** admin reads MRR as "$1,500/mo" when the
true exchange-adjusted figure is "$1,905" (or vice versa). This is a
business-metric bug, not a security one, but it directly mis-prices
churn risk and bonus accruals if any compensation is keyed off MRR.

**Recommended fix:**
- Audit the catalog source of truth and label its currency unambiguously
  (`price_usd_cents` vs `price_gbp_cents`).
- Either: store both currencies on `SUBPRODUCTS[*]` (fixed-rate cents),
  or: pick one canonical currency at the storage layer and only convert
  at the presentation layer with the exchange rate at the time of
  display.
- Reject `daily_cap_currency` values not in a hardcoded {"USD", "GBP"}
  set at the queries-layer write (current behaviour silently accepts
  whatever the UI sends because of the `allowed` allowlist letting any
  string through — `queries/markets.py:573-577`).

---

### MED-4 — Refund handling: not implemented; refund webhook events are silently ignored

**Locations:**
- `stripe_webhook_routes.py:282-298` (event-type dispatch)
- Entire repo `grep -rn "refund"` in `/Users/shocakarel/Habbig/gateway/*.py`
  returns **zero matches**.

**What:** The dispatch in `stripe_webhook` branches on
`customer.subscription.created`, `customer.subscription.updated`,
`customer.subscription.deleted`, `invoice.payment_failed`,
`invoice.paid` — and **silently logs-and-ignores anything else.**

```python
else:
    log.debug(
        "ignoring stripe event type=%s id=%s",
        event_type, event.get("id"),
    )
```

`charge.refunded`, `charge.refund.updated`, `invoice.payment_succeeded`,
`refund.created`, `refund.updated`, `customer.subscription.paused`,
`customer.subscription.resumed`, `subscription_schedule.*`,
`invoice.payment_action_required` — none are handled. The brief asks
about "refund double-application"; in this codebase, **refunds are
neither applied once nor twice** — they are not applied at all.

This is **not necessarily a bug** (the cancel handler flips status to
'cancelled' and the cancel-at-period-end UX never charges them again);
but it is a missing surface. If a Stripe-side refund issuance is the
intended trigger for narve-side access revocation, that wire is
disconnected. Conversely, if narve ever adds partial-refund logic
later, the existing webhook idempotency layer (`mark_received` →
`processed_stripe_events`) will defend against re-processing the *same*
refund event, but only because **`processed_stripe_events` is the only
idempotency layer**. There is no per-(subscription, refund_id)
deduplication beyond the event_id.

Brief asks specifically about "refund double-application". The answer
for this codebase: **a single refund event can only be processed once
thanks to `processed_stripe_events.event_id UNIQUE`**, BUT — if a future
handler is added — note that `mark_received` is INSERT OR IGNORE and
short-circuits on duplicate, which is correct; the watch-out is that the
INSERT happens BEFORE the dispatch try-block, so a handler that raises
DOES still leave the event marked-received. A retried Stripe event for
the same `event_id` will skip the dispatch. That's the right call for
state-mutation events but wrong for compensating actions: a refund handler
that failed midway needs a separate retry channel (e.g. a `pending_refunds`
table). Document this if/when the refund branch is added.

**Recommended fix:**
- Decide product policy: does a refund revoke access, or is access
  decoupled from payment refunds entirely? Most SaaS treat full refund as
  cancellation; partial refund changes nothing.
- Add an explicit `charge.refunded` (or `refund.created`) branch that
  either no-ops with a logged justification or calls the cancel path.
- Add a unit test that feeds a `charge.refunded` event and asserts the
  current intentional no-op.

---

### MED-5 — `_record_payment` silently no-ops when stripe_sub_id is missing or unmatched

**Location:** `stripe_webhook_routes.py:166-186` (`_record_payment`)

**What:**
```python
def _record_payment(event):
    obj = (event.get("data") or {}).get("object") or {}
    customer = obj.get("customer") or ""
    sub_id = obj.get("subscription") or ""
    if not sub_id:
        return
    with db.conn() as c:
        c.execute(
            "UPDATE subscriptions SET status = 'active' "
            "WHERE stripe_sub_id = ? AND status = 'past_due'",
            (sub_id,),
        )
```

If the `invoice.paid` event lacks a `subscription` field (e.g. a one-off
invoice not tied to a sub), the function returns 0 work done — fine. If
the `subscription` ID is present but doesn't match any row in our
`subscriptions` table (e.g. the user's `customer.subscription.created`
was missed or rejected upstream), the UPDATE matches zero rows and
silently exits — payment recorded as accepted on the Stripe side, no
corresponding access on the narve side. There is no audit row, no
counter, no log line above DEBUG.

Combined with HIGH-2's lack of customer-ownership cross-check, this means
a payment for a sub that the narve side never registered will quietly
succeed at the Stripe level with no narve-side correction path. The
nightly reconcile only walks users *with* `subproduct_subscriptions`
rows; a user whose subscription was never linked won't be visited.

**Recommended fix:** when `_record_payment` finds zero rows to update,
log at WARNING and insert into a `payment_orphans` table (sub_id,
customer, event_id, ts) so the admin can reconcile.

---

### LOW-1 — `processed_stripe_events.mark_processed` swallows DB errors silently

**Location:** `stripe_webhook_hardening.py:214-235`

**What:** `mark_processed` wraps its UPDATE in `try/except Exception: log.warning(...)`.
A DB error here means the event was *handled* but the
`processed_at`/`error` columns weren't updated. The next dispatch of the
same `event_id` will see the row exists (from `mark_received`) and
short-circuit to "already processed", so the actual partial-state event
never re-runs. Combined with HIGH-1's lack of atomicity, this means a
partially-handled event has no retry path.

**Recommended fix:** if mark_processed raises, *also* schedule a delayed
re-dispatch attempt (e.g. by writing an audit row to a separate, dedicated
table that the reconcile job picks up).

---

### LOW-2 — `cancel_subscription` queries-layer helper bypasses idempotency / rate-limit

**Location:** `queries/subscriptions.py:88-94`

```python
def cancel_subscription(user_id: int, dashboard_key: str) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE subscriptions SET status = 'cancelled' "
            "WHERE user_id = ? AND dashboard_key = ?",
            (user_id, dashboard_key),
        )
```

This is exposed via `db.cancel_subscription` (the queries module is
re-exported onto `db.py`). Any internal call site that uses it skips:
- `_billing_rate_limit` (per-user 20/hour cap)
- `with_idempotency` (cancel-retry collapse)
- the cancellation_attempts row (analytics)
- the win-back email enqueue (retention pipeline)
- the cache invalidate (`ttl_invalidate.on_subscription_change`)

Greppable callers should ALL go through the HTTP handler. Today the
helper is referenced from tests; if a new admin tool ever calls it
directly, it would silently bypass the retention/idempotency surface.

**Recommended fix:** rename to `_cancel_subscription_raw`, keep the
public `cancel_subscription` as a wrapper that requires a `reason`
argument and calls the full pipeline; or add a docstring + lint rule
discouraging direct use.

---

### LOW-3 — `get_user_intelligence_addon_active` lacks period-end ordering safety

**Location:** `queries/subscriptions.py:382-403`

```python
if row and row["intelligence_addon_active"]:
    if not row["intelligence_addon_period_end"] or row["intelligence_addon_period_end"] > int(time.time()):
        return True
```

This returns True when `period_end IS NULL`. If a misconfigured admin
flips `intelligence_addon_active=1` without setting `period_end`, the
user has permanent access. Compare to the trading add-on
(`queries/markets.py:467-471`) which has the same shape.

**Recommended fix:** require `period_end IS NOT NULL` for any
non-enterprise grant; document that NULL means "indefinite, by design"
and route those grants through `gifted_subscriptions` with
`is_permanent=1` rather than the user flag.

---

### LOW-4 — `list_subscriptions(user_id)` is the ownership check

**Location:** `queries/subscriptions.py:22-26`

**What:** All read helpers parameterise `user_id`, so a caller that
mis-passes a user_id can read another user's subs. This is the standard
pattern but it puts the *whole* ownership burden on every call site. The
audit verified that callers in `billing_routes.py` and
`stripe_webhook_*.py` use `user["user_id"]` from `current_user(request)`
(session-cookie-derived) — clean. But the helper itself is
unauthenticated; an internal tool that takes a `user_id` parameter from
a URL with no `current_user` check would IDOR.

**Recommended fix:** no immediate action — this is the SQLite-layer
contract — but consider a lint that flags any call site of
`list_subscriptions` / `has_active_subscription` etc. where the
`user_id` argument doesn't come from `current_user(...)`.

---

### Info findings

**INFO-1 — `db.conn()` opens a fresh connection per call.**
Acceptable for SQLite + WAL but means there is **no connection pool**;
nothing shares a transaction unless you explicitly thread a cursor.
Documented in `gateway/DB_HEALTH.md`. Reinforces HIGH-1.

**INFO-2 — Stripe IP allowlist snapshot dated 2026-05-14 in
`stripe_webhook_hardening.py:67`.** Refresh from
`https://stripe.com/files/ips/ips_webhooks.txt` per the comment. Not
in scope for this audit but worth flagging on the next quarterly check.

**INFO-3 — `_lookup_subproduct_slug` (`stripe_webhook_hardening.py:429-441`)
makes an outbound Stripe.Subscription.retrieve call during the webhook
handler.** If Stripe's API is slow, the webhook can't return 200 in time
and Stripe will retry. Combined with `mark_received` short-circuiting
retries, slow Stripe API responses can cause the metadata lookup to be
missed (the row is marked-received but the handler timed out before the
fetch returned). Consider moving slug-resolution out of the hot path:
either rely on metadata-on-the-invoice-directly, or back-fill via the
reconcile job.

---

## Methodology

1. Confirmed target file is absent via `find /Users/shocakarel/Habbig/gateway
   -iname "*billing*"`.
2. Identified the de-facto billing-queries layer:
   `queries/subscriptions.py`, `billing_routes.py`,
   `stripe_webhook_routes.py`, `stripe_webhook_hardening.py`,
   `jobs/reconcile_subscriptions.py`, `db.py` schema, and the per-user
   add-on helpers in `queries/markets.py`.
3. Walked each of the five attacker classes from the brief against those
   files; cross-referenced with existing tests
   (`tests/test_billing_portal.py`, `tests/test_settings_billing.py`,
   `tests/e2e/test_subscription_flow.py`).
4. For each finding, traced the code path end-to-end: HTTP entry →
   queries-layer helper → SQL → side effects → cache invalidation →
   webhook acknowledgement.
5. Compared against `SUBSCRIPTION_STATE_MACHINE.md`,
   `STRIPE_GO_LIVE.md`, `STATE_RECONCILIATION.md`, `RACE_CONDITIONS.md`
   in repo root for documented invariants. No conflicts found with this
   audit's claims; HIGH-1 and HIGH-2 are not flagged in any of those
   docs.

No code was changed. No tests were run. This is a read-only review of
the repo at `61180ce` on branch `feature/platform-build`.
