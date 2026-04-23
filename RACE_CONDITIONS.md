# Race conditions — what's protected, how, and where it isn't

Last updated: 2026-04-23

Companion to [`EDGE_CASES.md`](EDGE_CASES.md) Phase 7. This file
documents the specific SQL / code patterns we rely on for atomicity,
so the next contributor doesn't accidentally replace a safe pattern
with a TOCTOU (time-of-check vs time-of-use) bug.

SQLite's concurrency model matters here:
* One writer at a time (the WAL gives us one writer + N readers, not
  multiple writers).
* Statements are atomic at the statement boundary.
* Multi-statement work inside a single `with db.conn() as c:` block
  is inside an implicit transaction — commits on context-manager
  exit.

The gateway runs single-uvicorn-worker today. The patterns below
assume that; if we scale to `--workers N>1` we need to re-audit every
row with a CROSS-PROCESS call-out before trusting it.

---

## Invite-token claim — atomic in one UPDATE

**Scenario:** two users click "Redeem invite" on the same token within
milliseconds of each other.

**Protection:** the claim is a single UPDATE that bakes the
pre-condition into the WHERE clause.

```sql
UPDATE invite_tokens
   SET status            = 'claimed',
       claimed_by_user_id = ?,
       claimed_at        = ?
 WHERE token = ? AND status = 'unclaimed';
```

Exactly one UPDATE returns `rowcount = 1`; the other returns
`rowcount = 0` and the handler raises 409. There is **no
read-then-write** — the read is inside the UPDATE's WHERE.

**Never replace** with:

```python
row = c.execute("SELECT status FROM invite_tokens WHERE token = ?", (t,)).fetchone()
if row["status"] == "unclaimed":
    c.execute("UPDATE invite_tokens SET status='claimed' WHERE token=?", (t,))
```

Two callers can both read `unclaimed` before either writes.

---

## Subscription cancel + winback fan-out — idempotency ledger

**Scenario:** a user double-submits the cancel form. The legacy handler
would:
1. Flip `subscriptions.status = 'cancelled'` (idempotent, OK)
2. Call `_finalize_cancel_attempt` (idempotent on `attempt_id`, OK)
3. Call `_queue_winback_emails` — **fan-out, NOT idempotent**

Two submits = two sets of emails = a PR for support.

**Protection:** [`security.idempotency.with_idempotency`](gateway/security/idempotency.py)
wrapping step 3. Key: `(user_id, "billing_cancel_finalize", attempt_id)`,
TTL 30 s. Second submit replays the cached return value without
re-running the body.

**Caveat:** the idempotency store is in-process (or Redis when
configured). If we ever run multiple workers without Redis, two workers
could each pass the "not-yet-in-cache" check and both fan out. The
current single-worker deployment makes this non-issue, but the day we
scale workers we must:
1. Require `REDIS_URL` as a hard dependency, OR
2. Take a row-level SQL advisory lock in the same transaction.

---

## Webhook duplicate delivery — `processed_stripe_events` ledger

**Scenario:** Stripe retries a webhook for up to 3 days. We've seen
`customer.subscription.deleted` fire 4 times in an hour when a
previous delivery hit our 500.

**Protection:** the first line of the handler is
`INSERT OR IGNORE INTO processed_stripe_events (event_id, …)`. If the
UNIQUE constraint fires, `cur.rowcount == 0` and the handler returns
`{"status": "already_processed"}` with a 200 (so Stripe stops
retrying). See [`stripe_webhook_hardening.py`](gateway/stripe_webhook_hardening.py#L81-L116).

**Never replace** with a SELECT-then-INSERT check. The same TOCTOU
bug as invite-token claim.

---

## User deletion mid-request — graceful 401, never 500

**Scenario:** admin deletes user X while user X has an in-flight
request.

**Protection:** the session-cookie middleware looks up the user on
every request. If the row is gone, `request.state.user` becomes
`None`. Every handler that requires a user calls one of the
`_require_*` helpers, which raise
`HTTPException(status_code=401)` — never 500.

The deletion itself cascades via `ON DELETE CASCADE` FKs
(see schema dump in `EDGE_CASES.md` Phase 5), so the in-flight
handler's DB reads return empty / None for the user's related rows
rather than tripping a FK violation.

---

## Admin impersonation overlap — each gets its own cookie

**Scenario:** two admins both impersonate the same target user.

**Protection:** each `POST /admin/users/{id}/impersonate` mints a
fresh cookie token (`secrets.token_urlsafe`) and writes a new row to
`impersonation_sessions`. The two admins get distinct cookies; both
views work in parallel; the audit log records both starts and ends.
There is no "one impersonation at a time" lock — the business
explicitly wants two admins able to look at the same user
simultaneously (support + engineering on a Zoom, for example).

---

## Best-bets cache warm-up — single-flight (in-process)

**Scenario:** N concurrent requests all miss the TTL cache for
`best_bets:tier_pro:page_1`. Without protection, all N would run the
~800 ms DB query in parallel and each overwrite the cache entry.

**Protection:** the TTL cache uses a per-key `asyncio.Lock` (see
[`cache/ttl.py`](gateway/cache/ttl.py) — search for `asyncio.Lock`).
First request acquires the lock, computes, writes; others wait on
the lock and then read the now-populated entry on their second try.

**Single-process only.** Cross-worker single-flight would need Redis
SETNX with a short TTL — flagged for the day we scale.

---

## Feedback vote toggle — race between two tabs

**Scenario:** user opens the same feedback item in two tabs and
double-clicks upvote on both within milliseconds.

**Protection:** a UNIQUE index on `(user_id, feedback_id)` in
`feedback_votes`. Second INSERT fails with a constraint error that
the handler swallows; final vote count stays at +1.

**Not perfect:** if the two requests hit simultaneously, one of
them will *briefly* see `upvotes = N` after the first commit, then
fail its INSERT and return 409. The counter eventual-consistency is
acceptable for a vote widget — feel free to revisit if we ever
surface "last voter avatars" that would benefit from strict
ordering.

---

## Portfolio sync overlap — job-level mutex

**Scenario:** Polymarket sync cron fires at minute 11, and minute 21.
If sync 11 is still running at minute 21 (because Polymarket's API
was slow), we'd have two concurrent writers upserting into
`user_positions` for the same user.

**Protection:** the job runtime (`jobs/backend.py`) enforces one
in-flight instance per job name. A second fire no-ops with a
"previous instance still running" log line.

---

## Scraper → extraction → credibility pipeline

Three sequential stages, each independently re-entrant:

1. **Scrape** — INSERTs raw rows keyed on `(source_handle, external_id)`
   UNIQUE. Re-scraping the same post is a no-op.
2. **Extract** — SELECTs unextracted rows, calls Claude, UPDATEs back
   with extracted fields. Guarded by `extraction_status = 'pending'`
   flip on SELECT; second extractor's SELECT finds nothing to do.
3. **Credibility recompute** — recomputed from the full history table,
   so running it twice is idempotent (same inputs → same outputs).

---

## Known UNSAFE windows

### Concurrent profile edit in two tabs

Two tabs that both GET `/settings/profile`, edit different fields,
and both PATCH with their full body state → last-write-wins. The
tab that clicked Save second silently loses the other tab's edits.

**Accepted behaviour** for now — the business would rather accept
this than ship an "edit conflict" modal on a settings page users
rarely open. If it becomes a real issue:
* Add an `If-Match: <etag>` header derived from the row's updated_at.
* Return 412 when the etag is stale; UI reloads.

### Concurrent saved-note edit

Same shape as profile. Not a user-reported issue today.

### Gift-subscription transfer mid-cancel

If a user cancels their own sub at the exact moment a gift they sent
is accepted by the recipient, the cancel + gift-accept hit different
Stripe subscriptions (by design — gifts are separate Stripe records)
so nothing actually races at the data layer. Documented here only so
reviewers don't mistakenly add a "lock" around the gift flow.

---

## If you're adding a new write

Before merging:

1. Is the pre-condition baked into the UPDATE's WHERE, not checked
   first in a separate SELECT? (invite-token pattern)
2. If the write triggers an email / payment / external call, is it
   wrapped in `with_idempotency(...)` or an equivalent ledger?
3. If the write happens inside a job, is the job enforcing single-
   in-flight at the registry level?
4. If two users could legitimately race (vote, claim, invite accept),
   is there a UNIQUE index that makes the loser's INSERT fail
   predictably?

Answer yes to each before approving the PR.
