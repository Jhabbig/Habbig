# Stripe Webhook Idempotency Audit

Date: 2026-05-15  
Auditor: Claude (Opus 4.7)  
Targets:
- `/Users/shocakarel/Habbig/gateway/stripe_webhook_routes.py`
- `/Users/shocakarel/Habbig/gateway/stripe_webhook_hardening.py`
- `/Users/shocakarel/Habbig/gateway/migrations/061_processed_stripe_events.py`

Driver: `/Users/shocakarel/Habbig/audits/_audit_stripe_idempotent.py`. Re-run with:

```
python3 /Users/shocakarel/Habbig/audits/_audit_stripe_idempotent.py
```

## Brief

Stripe retries failed webhook deliveries with exponential backoff for up to
3 days. The handler MUST be idempotent under retry: the same `evt_*` ID
delivered 5 times must result in exactly **1 grant** in the `subscriptions`
table, not 5.

Idempotency layer under test:

1. `mark_received(event)` — `INSERT OR IGNORE` into `processed_stripe_events`
   (UNIQUE on `event_id`). If the INSERT was a no-op the function returns a
   `JSONResponse({'status': 'already_processed'})` so the route short-circuits
   with 200 (no Stripe retry storm).
2. Dispatch branch (`_grant_access` / `_update_plan` / `apply_*`) runs only on
   the first delivery.
3. `mark_processed(event, error=...)` stamps `processed_at` at the end of the
   branch — both on success and on caught exception.

Defence-in-depth: the `subscriptions` table has `UNIQUE(user_id, dashboard_key)`
and the INSERT in `_grant_access` is `ON CONFLICT … DO UPDATE`, so even if the
event-ID layer failed open, distinct events for the same subscription would
still collapse to a single row.

## Verdict

- Scenarios run: **6**
- Passed: **6**
- Failed: **0**

**Result: PASS** — the brief's hard
requirement (`same event delivered 5x ⇒ 1 grant, not 5`) is met by scenario 1.

### 1) 5x identical delivery → 1 grant

- Pass: **YES**
- verdicts: `['processed', 'already_processed', 'already_processed', 'already_processed', 'already_processed']`
- subscriptions_row_count: `1`
- ledger_total: `1`
- ledger_processed: `1`

### 2) 5 distinct events, same sub → 1 row (UPSERT)

- Pass: **YES**
- verdicts: `['processed', 'processed', 'processed', 'processed', 'processed']`
- subscriptions_row_count: `1`
- ledger_total: `5`
- ledger_processed: `5`

### 3) crash mid-handler → replay short-circuits

- Pass: **YES**
- second_attempt: `already_processed`
- subscriptions_row_count: `0`
- ledger_error: `simulated crash inside _grant_access`
- ledger_processed_at_set: `True`

### 4) mark_received swallows DB errors → returns None

- Pass: **YES**
- response: `None (handler proceeds)`

### 5) concurrent deliveries → 1 first, 1 short-circuit

- Pass: **YES**
- results: `['first', 'short_circuit']`
- first_count: `1`
- short_circuit_count: `1`

### 6) missing event ID → handler runs but cannot dedupe

- Pass: **YES**
- first_call: `processed`
- second_call: `processed`
- subscriptions_row_count: `1`
- ledger_total: `0`

## Gaps

Hard-rule gaps surfaced by the audit. Each gap is a real risk that the brief's
test (`5x ⇒ 1 grant`) does NOT exercise; reviewers should triage these before
declaring the webhook fully idempotent against Stripe's full retry surface.

### GAP-1 — `mark_received` early-returns on missing `event.id`

**Location:** `stripe_webhook_hardening.py:192-193`.

```python
if not event_id:
    return None  # unexpected shape; let the handler deal with it
```

Stripe ALWAYS sends `id`, so in practice this branch is unreachable from real
traffic. But if a malformed/forged event sneaks through signature verification
(or a test fixture omits `id`), the handler runs without an idempotency record,
so the same event re-delivered re-runs the dispatch branch. For
`customer.subscription.created` the `UNIQUE(user_id, dashboard_key)` defence
still bounds the damage to 1 row, but for
`apply_subscription_cancelled` it would re-revoke sessions, re-deactivate
widgets, and re-enqueue the cancellation email on every delivery.

**Fix:** treat `id` as required — log and `return JSONResponse(400)` when
missing, mirroring the signature-failure branch.

### GAP-2 — `mark_received` DB errors fall open (no ledger row)

**Location:** `stripe_webhook_hardening.py:209-211`.

```python
except Exception as exc:
    log.warning("stripe idempotency record failed: %s", exc)
return None
```

By design — if the idempotency table is unavailable, the handler still
processes the event so we don't *miss* it on retry. But it ALSO does not
record the event, so the next retry (DB now back) writes a fresh ledger row
and re-dispatches. Same risk shape as GAP-1: bounded for the create branch
by the UPSERT, but unbounded for branches with non-DB side effects (email
enqueue, session revoke).

**Fix:** wrap the dispatch branches that fan out to non-DB systems
(`apply_subscription_cancelled` enqueues email; `_record_payment` is
DB-only and safe) in a second idempotency check keyed on `event_id` so a
missed ledger write doesn't translate into duplicate side effects.

### GAP-3 — Side effects after a crash are not retried

**Location:** `stripe_webhook_hardening.py:198-204` + `stripe_webhook_routes.py:300-307`.

Sequence: `mark_received` writes the row, dispatch starts, crashes halfway
through. `mark_processed(..., error=...)` stamps `processed_at`. On Stripe's
retry, `mark_received` short-circuits because the row exists — the second
attempt **never runs**, so any side effect that was supposed to run after the
crash point is permanently lost.

For `_grant_access` this is a non-issue: the entire DB write is a single
`INSERT … ON CONFLICT`; it either ran or it didn't. For
`apply_subscription_cancelled` (which has 4 distinct side effects: subproduct
status, session revoke, widget deactivate, email enqueue) a partial failure
leaves the system in an inconsistent state with no automated remediation.
Operators must read the admin panel's `error IS NOT NULL` rows and replay
manually.

**Fix:** Either (a) make every dispatch branch a single transaction with the
ledger write so a crash rolls both back and lets Stripe retry; or (b) move
the `mark_received` write to AFTER the dispatch branch succeeds (sacrificing
crash-in-flight idempotency for crash-survivability).

### GAP-4 — `mark_processed` runs even when `mark_received` short-circuits is unreachable

**Location:** `stripe_webhook_routes.py:278-308`.

Reading the route carefully: when `mark_received` returns a short-circuit
response, the route `return`s immediately (line 279), so `mark_processed` at
the bottom is bypassed for replayed events. Not a bug — but worth noting:
the `processed_at` timestamp on a ledger row only reflects the FIRST
successful dispatch, not the 4 retries that followed. The admin panel showing
`received_at` for a row will see the original delivery time only — this is
correct, but easy to misread when triaging Stripe replay storms.

**Fix:** none required. Documented here so the next operator reading the
ledger doesn't assume retries are missing.

### GAP-5 — No retention policy on `processed_stripe_events`

**Location:** `migrations/061_processed_stripe_events.py`.

Stripe's retry window is 3 days. After that, the same `evt_*` ID will never
re-arrive, so the ledger row is only useful for forensic admin queries. The
table grows unbounded — at ~3-5 events per active subscriber per month, this
becomes a multi-GB table after a few years.

**Fix:** add a janitor cron (or migration) that deletes rows where
`received_at < (now - 30 days) AND error IS NULL`. Errored rows should be
retained for the audit log.

### GAP-6 — Concurrency under the gateway's shared SQLite connection

**Location:** `db.conn()` returns a shared connection. Scenario 5 exercises
two threads against the same event_id; SQLite's per-connection write lock
serialises them, so the UNIQUE constraint reliably enforces single-grant.

Risk: if the gateway is ever moved off SQLite (e.g. to Postgres for HA), or
if the shared-connection pattern is replaced with per-request connections,
the race window between `mark_received`'s `INSERT OR IGNORE` and the
dispatch branch widens. Two concurrent retries could both pass `mark_received`
if they used the SAME `INSERT OR IGNORE` row (one wins, one short-circuits)
— the UNIQUE still saves us. But a poorly-coded refactor that swapped the
INSERT for a SELECT-then-INSERT would expose the race fully.

**Fix:** keep `INSERT OR IGNORE`. Add a comment explicitly noting that the
UNIQUE constraint is the load-bearing primitive, not the SELECT-then-act
pattern.

## Method

Each scenario runs in its own tempfile SQLite DB, freshly migrated to head.
The driver re-uses the production `mark_received` / `mark_processed` helpers
from `stripe_webhook_hardening.py` and mirrors the `_grant_access` /
`_update_plan` SQL from `stripe_webhook_routes.py` verbatim so the audited
behaviour matches what the FastAPI route actually does. The route's FastAPI
scaffolding (signature check, IP allowlist, livemode gate) is **out of
scope** here — covered separately in `audit_stripe_webhook.md`.

Synchronous bash only per the brief's hard rule; pre-release endpoints are
untouched (no `prerelease` paths are exercised, no environment flag is set
that would change pre-release behaviour).

