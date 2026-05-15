# Adversarial audit: `gateway/queries/subproducts.py`

**Date:** 2026-05-15
**Auditor focus areas (per request):**
1. Subscription-state ownership check
2. Gift-subscription transfer atomicity
3. Sub-tier upgrade race
4. Paused-until enforcement at read time

---

## Scope note (read this first)

The requested path `gateway/queries/subproducts.py` **does not exist** in the
tree. Confirmed via `find /Users/shocakarel/Habbig -name 'subproducts.py'`
and a listing of `gateway/queries/`. The only file in that directory that
matches the requested focus areas (subscription state, gift handling,
tier resolution, pause windows) is:

> `/Users/shocakarel/Habbig/gateway/queries/subscriptions.py` (585 LOC)

Treating the prompt path as a typo and auditing **`subscriptions.py`**.
Audit findings are written against that file, line numbers reference it.

The "subproduct" namespace is a separate concern (catalogue + access
gating) and lives in:

- `gateway/subproduct.py` (catalogue, `is_user_subscribed_to`)
- `gateway/subproduct_access.py` (access enforcement wrappers)
- `gateway/middleware/subproduct.py` (per-host routing)

None of those expose the four focus areas the request listed; they
delegate ownership/state checks back to the query helpers under audit
here.

---

## Storage / transaction context

- DB: SQLite via `db.conn()` (`gateway/db.py:258`) — no `WAL` mode, no
  pragma `busy_timeout`, default `isolation_level=""` (deferred BEGIN
  on first DML).
- `subscriptions` table (`gateway/db.py:46`): unique key
  `(user_id, dashboard_key)`. No `paused_until`, no `current_period_end`,
  no row-level version column. `expires_at` is the only time-bounding
  field.
- `gifted_subscriptions` table (`gateway/db.py:386`): `user_id` is the
  beneficiary, no separate "claimed_by" / "redeemed_by" / "transfer_to"
  column. There is no gift-transfer API in this module — gifts are
  created by admin and revoked by admin only.
- `users.subscription_paused_until` lives on the `users` table (added
  in migration `094_cancellation_flow.py`); the `subscriptions.py`
  query helpers **never read it**.

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 3 |
| Medium   | 4 |
| Low      | 3 |
| Info     | 2 |

**Total findings: 12**

---

## Top 3 (most important to fix)

1. **H-01 — `has_active_subscription` ignores `users.subscription_paused_until`.**
   Read-time access checks do not consult the pause window; a user
   whose subscription was paused via the cancellation flow still
   passes gating until `expires_at` arrives. This is the entire point
   of the pause feature and it is silently bypassed at every call-site
   that gates a dashboard.

2. **H-02 — `upsert_subscription` is not atomic with the referral
   conversion hook.** The `INSERT ... ON CONFLICT` commits implicitly
   on the next outer `with` exit, then `db_referrals.mark_referral_converted`
   is called *after* the connection block — they run in two transactions
   on two connections. A crash or worker termination between the two
   leaves the user subscribed but un-attributed (and a partial retry
   double-grants referral rewards if the job re-runs). The bare
   `except Exception` already swallows failures here.

3. **H-03 — Tier upgrade race in `upsert_subscription` under concurrent
   Stripe webhook redelivery.** `ON CONFLICT DO UPDATE` overwrites
   `plan`, `started_at`, `expires_at`, `stripe_sub_id`, and `source`
   with `excluded.*` unconditionally. SQLite default deferred
   transactions plus no `busy_timeout` plus two webhooks delivered in
   parallel (Stripe routinely retries on 5xx) mean an older
   `customer.subscription.updated` event can clobber a newer
   `invoice.paid` upgrade with a downgrade. There is no
   `WHERE excluded.started_at > subscriptions.started_at` guard and
   no stripe `event.id` idempotency table consulted here.

---

## Findings — detail

### H-01 — Paused-until is never enforced at read time
**Lines:** `subscriptions.py:29–44` (`has_active_subscription`),
`subscriptions.py:514–536` (`has_any_active_subscription`),
`subscriptions.py:539–559` (`get_user_active_subproducts`),
`subscriptions.py:424–440` (`get_user_subscription_tier`).

`subscription_paused_until` is set by the cancellation/pause flow
(`gateway/billing_routes.py:991`) and cleared at expiry by
`gateway/server.py:3978`. None of the read-side gates in this
module consult that column. A paused user retains:

- Per-dashboard access (`has_active_subscription` returns True while
  `expires_at` is still in the future).
- Cross-dashboard features behind `has_any_active_subscription`
  (embed widgets via `embed_routes.py:130/137/769`, saved views via
  `saved_views_routes.py:67`).
- Pro tier in `get_user_subscription_tier`.

**Impact:** the pause UX promised in billing (no charges, no service)
delivers half of the contract — billing pauses, service continues.
For an admin-issued pause this also defeats abuse-mitigation use cases.

**Fix shape:** add a `users.subscription_paused_until > now()` short
circuit at the top of `has_active_subscription` and
`has_any_active_subscription`, and exclude paused users from
`get_user_active_subproducts`. The check needs to be in this module
because every gating call-site goes through it.

---

### H-02 — Cross-transaction split between subscription write and
referral marker
**Lines:** `subscriptions.py:57–85`.

```python
with db.conn() as c:
    c.execute("INSERT INTO subscriptions ... ON CONFLICT DO UPDATE ...")
# ← commit happens here on context exit
try:
    import db_referrals as _dbr
    _dbr.mark_referral_converted(user_id)   # second tx, second conn
except Exception:
    _logging.getLogger("db").exception(...)   # silently swallowed
```

Two distinct connections, two distinct transactions. Failure modes:

1. Process crash between the two writes → user has a paid sub, but
   the referrer never gets their reward; the nightly job has no way
   to retroactively detect "this user converted but we missed it"
   because the marker is the only signal.
2. `mark_referral_converted` is idempotent only if its caller passes
   a stable key. Code that retries `upsert_subscription` (e.g. webhook
   redelivery, see H-03) calls `mark_referral_converted` again → if
   the marker is not strictly idempotent this double-credits.
3. The `except Exception` swallows *all* errors including
   `sqlite3.OperationalError("database is locked")`, which is
   precisely the error class that retries would have recovered from.

**Fix shape:** either fold the referral marker into the same `with
db.conn() as c:` block (preferable, single transaction), or move the
referral hook to an outbox table written in the same transaction and
processed by a separate worker.

---

### H-03 — Tier-upgrade race on concurrent webhook redelivery
**Lines:** `subscriptions.py:55–72`.

```sql
INSERT ... ON CONFLICT(user_id, dashboard_key) DO UPDATE SET
    plan        = excluded.plan,
    status      = 'active',
    started_at  = excluded.started_at,
    expires_at  = excluded.expires_at,
    stripe_sub_id = excluded.stripe_sub_id,
    source      = excluded.source
```

There is no monotonicity guard. Realistic attack/incident scenarios:

1. Stripe sends `customer.subscription.updated` (downgrade, e.g. plan
   change scheduled later) and `invoice.paid` (current upgrade)
   close together. Either can win the race.
2. Replay: a malicious actor with access to a captured webhook
   payload re-POSTs an old `customer.subscription.updated` event for
   plan=trader after the user upgraded to pro. The handler ultimately
   calls `upsert_subscription`, which overwrites pro back to trader.
   The Stripe `event.id` is replay-protected at the webhook layer
   (verify there!) but this helper does not defend in depth.
3. Two webhook workers consume the queue in parallel under FastAPI
   workers; SQLite default `isolation_level=""` opens a deferred
   transaction, both succeed, last writer wins, the loser's update
   is silently dropped.

The downstream caller (`upsert_subscription` consumers in
`server.py:1999/4464/4495/4530/6138`, `server_features.py:1624/1630`)
does not pre-check the current row, so the race is fully reachable.

**Fix shape:** add a server-side ordering check:

```sql
ON CONFLICT(user_id, dashboard_key) DO UPDATE SET
    plan        = CASE
                    WHEN excluded.started_at >= subscriptions.started_at
                    THEN excluded.plan ELSE subscriptions.plan END,
    ...
```

Plus a `stripe_event_id` idempotency table consulted by the caller, or
move the whole helper to `BEGIN IMMEDIATE` and add `busy_timeout=5000`.

---

### M-01 — `cancel_subscription` performs no ownership / status check
**Lines:** `subscriptions.py:88–94`.

```python
def cancel_subscription(user_id: int, dashboard_key: str) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE subscriptions SET status = 'cancelled' "
            "WHERE user_id = ? AND dashboard_key = ?",
            (user_id, dashboard_key),
        )
```

No `RETURNING`, no row count check, no current-status guard. Effects:

- Cancelling an already-cancelled row silently "succeeds" (no signal
  to the caller).
- Cancelling a `(user_id, dashboard_key)` that doesn't exist silently
  "succeeds" — caller cannot distinguish "nothing happened" from
  "actually cancelled".
- Cancelling a row that was already `expired` flips its status back
  to `cancelled`, which `get_revenue_stats` (line 300) and
  `get_churn_rate` (line 218) then count as fresh churn.

**Fix shape:** check `c.rowcount` and require `status = 'active'` in the
WHERE clause, return a bool.

---

### M-02 — `get_user_active_gifts` does not enforce a `starts_at` lower bound
**Lines:** `subscriptions.py:364–371`.

```sql
WHERE user_id = ? AND revoked = 0
  AND (is_permanent = 1 OR ends_at IS NULL OR ends_at > ?)
```

A gift with `starts_at` in the future (an admin scheduling a future
gift) is treated as already-active. The schema permits `starts_at`
to be any integer, and `create_gift` (line 342) blindly writes
`int(time.time())` — but a future migration or admin tooling that
sets `starts_at` to a future timestamp is silently bypassed at
read time.

**Fix shape:** add `AND (starts_at IS NULL OR starts_at <= ?)` and pass
`now` twice.

---

### M-03 — `revoke_gift` does not check that the gift exists or is not
already revoked
**Lines:** `subscriptions.py:374–379`.

`UPDATE … SET revoked = 1, revoked_at = ?, revoked_by_admin_id = ?
WHERE id = ?` — no current-state guard. Re-revoking overwrites the
original `revoked_at` and `revoked_by_admin_id`, destroying audit
trail. A request for a non-existent gift_id silently succeeds.

**Fix shape:** `AND revoked = 0` plus `rowcount` check, return bool.

---

### M-04 — `set_user_intelligence_addon` caches invalidation is
best-effort but the state change is permanent
**Lines:** `subscriptions.py:406–421`.

The DB write succeeds first, then cache invalidation runs inside a
bare `except Exception`. If invalidation fails (cache pod down,
network blip), the user's tier has shifted in the DB but the cached
tier-scoped feeds and best-bets pages remain stale for the full
TTL. Result: a user gets / loses Intelligence access in the DB but
the UI keeps showing the old state. Exception is logged but not
surfaced to the caller — no signal to retry, no health metric.

**Fix shape:** at minimum, emit a counter metric for cache-invalidation
failures so SRE can detect drift. Better: invert the order (invalidate
first, write second) or use a proper invalidation outbox.

---

### L-01 — `has_active_subscription` makes two queries when one would do
**Lines:** `subscriptions.py:29–44`.

Admin check is a separate `SELECT is_admin FROM users` before the
subscription check. Every gate call (server.py × 5 sites) pays the
cost. Could be a single `SELECT ... FROM users u LEFT JOIN
subscriptions s ON ... WHERE u.id = ?` and let the call short-circuit.
Not a correctness bug, just two round-trips where one suffices on
the hot path.

---

### L-02 — `get_user_active_subproducts` returns admins an empty set
silently
**Lines:** `subscriptions.py:539–559`.

Docstring says "Admins return an empty set here on purpose: callers
that want 'show everything' treat Pro / admin specially before calling."
There is no admin pre-check inside the function — admins genuinely
return whatever happens to be in `subscriptions` for their user_id,
which is usually empty. Any caller that *forgets* the admin pre-check
silently fails-closed for admins. The docstring promises a behavior
the implementation doesn't actually enforce. Cosmetic but
trap-laden.

---

### L-03 — `get_user_subscription_tier` ignores `expires_at`
**Lines:** `subscriptions.py:424–440`.

```sql
SELECT plan FROM subscriptions WHERE user_id = ? AND status = 'active'
```

No `AND (expires_at IS NULL OR expires_at > ?)`. A subscription whose
`status` was never flipped to `cancelled` but whose `expires_at`
passed is still counted as "pro" / "trader" here. Compare with
`has_active_subscription` (line 38) which gets this right. Most
real-world rows have status flipped on cancel, but rows with a
hard expiry (gift-converted, comped, manual placeholder) are
permanently mis-tiered until status is manually cleaned up.

---

### I-01 — Gift transfer is genuinely not implemented
**Schema:** `gateway/db.py:386–402`.

The request asked for an audit of gift-subscription *transfer*
atomicity. There is no transfer API in this module: gifts are
created with a fixed `user_id` (line 332–349) and revoked
in-place (line 376). There is no `reassign_gift`, no
`transfer_gift_to_user`, no claim-token flow. If a transfer
feature is on the roadmap, the schema needs at minimum:

- A `claimed_by_user_id` column distinct from `user_id` (the buyer).
- A `claim_token` and `claimed_at`.
- An atomic transaction wrapping (validate token) + (assign
  beneficiary) + (mark token consumed).

Flagging this as info because the absence is the finding.

---

### I-02 — `list_active_gifts` is unpaginated
**Lines:** `subscriptions.py:353–361`.

Returns every active gift row in one shot. For an admin panel today
this is fine; left here as a note since the perf audit on
`list_all_subscriptions` (line 97) already moved to cursor
pagination and the same treatment will be wanted here once the
gift inventory grows.

---

## Methodology

- Read `gateway/queries/subscriptions.py` end to end.
- Cross-checked schemas in `gateway/db.py` (subscriptions table at
  line 46, gifted_subscriptions at line 386).
- Cross-checked the absence of `paused_until` on `subscriptions` by
  full grep — column lives on `users` and is read only from
  `server.py` and `billing_routes.py`, never from this module.
- Mapped every `db.has_active_subscription`, `db.has_any_active_subscription`,
  `db.upsert_subscription`, `db.cancel_subscription`,
  `db.revoke_gift`, `db.set_user_intelligence_addon` call-site outside
  of tests to confirm reachability of each finding.
- No code changes performed, no fix patches written — audit only.
