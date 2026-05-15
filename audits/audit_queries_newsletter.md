---
target: gateway/queries/newsletter.py
introduced_commit: 992005b (security(audit#12 MED#1): bound /admin/newsletter/send recipient loop)
audited_by: Claude Opus 4.7 (1M context)
audited_on: 2026-05-15
scope: adversarial review of bounded-blast (deferred-tail) data plane
adjacent_files:
  - gateway/admin_routes.py (newsletter_send handler)
  - gateway/jobs/newsletter_blast_jobs.py (cron worker)
  - gateway/migrations/187_newsletter_blast_jobs.py (table schema)
  - gateway/db.py (conn(), re-exports)
methodology: |
  Static read-through with adversarial framing — race conditions, SQL injection,
  double-spend, filter drift, integer-overflow / off-by-one, scheduler-replay,
  multi-worker concurrency. No instrumentation; no test execution.
---

# Audit — `gateway/queries/newsletter.py` (bounded-blast helpers)

## Severity counts

| Severity | Count |
|---|---|
| Critical | 0 |
| High     | 2 |
| Medium   | 3 |
| Low      | 4 |
| Info     | 2 |

## Top 3

1. **HIGH-1 — Double-spend across inline / deferred boundary when subscriber
   count shrinks between handler write and first tick.** Inline portion enqueues
   id-sorted page `[0, inline_target)`. Tick worker recomputes
   `inline_count = count_blast_recipients(...) - total_recipients` to derive its
   offset. If any of those inline-portion subscribers unsubscribe (or are
   deleted) between the POST and the first tick, `count_blast_recipients` drops
   by N, the derived `inline_count` drops by N, the tick offset slides back by
   N, and the tick re-enqueues the N rows that took the inline slots vacated by
   the unsubscribers. Same recipients receive the blast twice.
2. **HIGH-2 — No `SELECT ... FOR UPDATE` / row-claim around `mark_blast_job_started`;
   two worker processes can both flip the same row to `running` and double-send
   the whole tail.** APScheduler's `max_instances=1` only guards against a
   second instance *inside the same process*. Multi-worker deploys (the codebase
   gates on `NARVE_SCHEDULER_LEADER=1`, see scheduler/scheduler.py:235) work
   correctly only if the operator sets it correctly. A misconfiguration silently
   reverts to "every uvicorn worker runs the tick" — each worker picks the same
   `fetch_next_pending_blast_job` row, both run the full batch, and every
   recipient in the tail gets duplicate sends. The `UPDATE ... WHERE
   status IN ('pending', 'running')` in `mark_blast_job_started` is *not* a lock
   — it's two non-atomic statements (SELECT in `fetch_next_pending_blast_job`
   then UPDATE) that race trivially.
3. **MED-1 — `advance_blast_job_progress` reads-after-write inside the same
   connection without any concurrency control; if the deferred-tail worker is
   ever sharded by campaign_id (mentioned as a future direction), two ticks
   acting on different jobs that both happen to call `advance_blast_job_progress`
   on the same row id will race the read after the increment, with the loser
   observing stale `processed_recipients` and possibly mis-firing the
   `status='done'` transition.** Today the cron is single-threaded, so this is
   latent — but the function's contract advertises "returns the row" without
   warning callers that the read isn't atomic w.r.t. concurrent writers.

---

## Findings

### HIGH-1 — Inline/deferred double-spend on subscriber churn

**Where:**
- `newsletter.py:572` (`get_blast_recipients_page`)
- `jobs/newsletter_blast_jobs.py:112` (offset derivation)
- `admin_routes.py` (commit 992005b lines 2820-2895)

**Mechanism.** The handler writes the table:

```
recipient_count           = count_blast_recipients(seg, freq)
inline_target             = min(recipient_count, 500)
deferred_target           = recipient_count - inline_target
campaign.recipient_count := recipient_count
job.total_recipients     := deferred_target
job.processed            := 0
inline_rows              = page(offset=0, limit=inline_target)   # enqueue inline
```

Then on the first tick (could be 60 s later, or an hour later if the scheduler
was paused):

```python
# jobs/newsletter_blast_jobs.py:112-119
inline_count = (
    db.count_blast_recipients(segment=..., frequency_filter=...)
    - total                     # total = job.total_recipients (deferred_target)
)
offset = max(0, inline_count) + processed
```

Let `R = original recipient_count`, `D = deferred_target`, `I = inline_target = R - D`.
Live subscriber count `R_now` may differ by the tick time.

If `R_now < R` (anyone unsubscribed in the gap), then
`inline_count = R_now - D < I`. The tick reads page
`[R_now - D + 0, R_now - D + batch_size)`. Those rows overlap with the
inline portion `[0, I)` whenever `R_now - D < I`, i.e. whenever any
inline-portion subscriber unsubscribed. Each subscriber whose id falls in the
overlap range gets enqueued **twice**.

**Worked example.** R = 600, D = 100, I = 500. Five inline subscribers
unsubscribe immediately after the POST. R_now = 595. Tick computes
`inline_count = 595 − 100 = 495`. Tick fetches page
`[offset=495, limit=100)` — but rows 495..499 were already enqueued inline.
Five duplicate sends.

**Why the existing "live count drift" comment is wrong.** The docstring
on `newsletter_blast_jobs.py:104-111` claims the drift case is benign — "fewer
rows are returned". That's only true when the offset *advances past* the live
end (R_now < total + I). When R_now drops below R but stays above D, the
offset slides *backward into the inline range*, double-enqueueing.

**Why `unsubscribed_at IS NULL` doesn't save you.** The unsubscribers
themselves are filtered out — but their position-by-id is filled by the *next*
ids in the result set. Page offset is rank-positional, not id-positional;
shrinking the set shifts every higher id leftward.

**Suggested fix.** Stop using OFFSET-based paging across an unstable result
set. Either:
- Store the `max_id_at_blast_time` on the campaign row and page by
  `WHERE id > last_processed_id` (cursor pagination, immune to churn), or
- Materialise the recipient list at handler time into a join table
  (`newsletter_blast_recipients(job_id, subscriber_id, status)`) and have the
  tick worker pull from that snapshot.

### HIGH-2 — Multi-worker double-tick race on `fetch_next_pending_blast_job` → `mark_blast_job_started`

**Where:**
- `newsletter.py:633-647` (`fetch_next_pending_blast_job`)
- `newsletter.py:650-663` (`mark_blast_job_started`)
- `jobs/newsletter_blast_jobs.py:85`

**Mechanism.** The handler-to-worker transfer is two separate
`db.conn()` blocks:

```python
# Worker A
job = db.fetch_next_pending_blast_job()   # SELECT ... LIMIT 1
# ... arbitrary delay (could be µs, could be a context switch ...)
db.mark_blast_job_started(job_id)         # UPDATE ... WHERE status IN ('pending','running')
```

Between SELECT and UPDATE, worker B can run the exact same SELECT and observe
the exact same `pending` row. Both UPDATEs succeed (the row is still `pending`
when each one fires; even after one has flipped it to `running`, the WHERE
clause includes `running`). Both workers proceed to enqueue
`MAX_BATCH_PER_TICK` recipients each, double-fanning every page in the tail.

`advance_blast_job_progress` also races trivially: both workers do
`UPDATE ... SET processed = processed + 500 WHERE id = ?`. SQLite serialises
the two UPDATEs so `processed` ends at +1000, which mis-claims they made twice
the progress. Result: a 100k tail finishes "early" with 50k recipients sent
twice and 50k never sent (the `processed >= total` check fires after each
worker's own +500).

**Why APScheduler `max_instances=1` doesn't save you.** That's a per-process
guard. Multi-worker uvicorn deploys (which the codebase explicitly supports —
`NARVE_SCHEDULER_LEADER=1` env flag) require the operator to set the flag on
exactly one worker. A misconfig — flag forgotten, flag set on two workers,
flag set on N workers during a rolling restart — turns every worker into a
tick driver. There is no DB-side mutex to catch this.

**Recommendation.** Either:
- Use a CTE-style atomic claim: `UPDATE ... SET status='running' WHERE
  id = (SELECT id FROM ... WHERE status='pending' LIMIT 1) AND status='pending'
  RETURNING id` (SQLite 3.35+ has RETURNING), or
- Take an exclusive transaction (`BEGIN IMMEDIATE`) around the SELECT+UPDATE
  pair in `fetch_next_pending_blast_job` itself, returning the row only after
  it's been atomically flipped to `running`. The current shape — SELECT in one
  txn, UPDATE in another — is the textbook double-claim pattern.

### MED-1 — `advance_blast_job_progress` re-reads non-atomically within the same connection

**Where:** `newsletter.py:666-702`

The function does:

```sql
UPDATE newsletter_blast_jobs SET processed_recipients = processed_recipients + ? WHERE id = ?
SELECT ... FROM newsletter_blast_jobs WHERE id = ?
-- if row["processed_recipients"] >= row["total_recipients"]:
UPDATE newsletter_blast_jobs SET status='done', finished_at=? WHERE id = ?
SELECT ... FROM newsletter_blast_jobs WHERE id = ?
RETURN dict(row)
```

All inside one `db.conn()` context (one implicit transaction), so within a
single tick this is consistent. The MED comes from two issues:

1. **No `BEGIN IMMEDIATE`** — under default SQLite isolation, the first DML
   (`UPDATE`) acquires a `RESERVED` lock. If another writer is mid-transaction
   on the DB at that moment, the UPDATE retries or errors with
   `SQLITE_BUSY`, surfacing as an uncaught `sqlite3.OperationalError` rather
   than the expected dict. Caller (the tick) treats that as the page-fetch
   error path and marks the entire job `failed` — losing the remainder of a
   90% complete blast on transient lock contention. WAL mode helps but doesn't
   eliminate this; any concurrent admin write (e.g. a campaign INSERT) can
   block.
2. **Status transition isn't a single conditional UPDATE** — the close-on-
   completion is implemented as `SELECT … if condition: UPDATE`. Under the
   HIGH-2 race, two workers can each pass the `processed >= total` check and
   both UPDATE `status='done'` (idempotent here, but the `finished_at`
   timestamp races and the audit timeline shows whichever worker wrote last).

**Recommendation.** Collapse the conditional into a single UPDATE:

```sql
UPDATE newsletter_blast_jobs
SET status = 'done', finished_at = ?
WHERE id = ? AND status != 'done' AND processed_recipients >= total_recipients
```

…and either wrap the increment+close in `BEGIN IMMEDIATE` or move both into a
single `WITH … UPDATE` CTE so they share a snapshot.

### MED-2 — Page-fetch SQL parameterisation is safe, but column-name interpolation in `get_blast_recipients_page` builds the WHERE clause from segment/frequency in a way that *passes today* but invites future SQLi

**Where:** `newsletter.py:572-608`

**Status.** Currently safe. The function calls
`if seg != "all": where.append("(segment = ? OR segment = 'all')")` —
the segment is bound as a `?` parameter; the literal `'all'` is hard-coded
inside the SQL string. Frequency goes through the same `?` pattern. No
user-controlled string interpolation.

**Why it's MED rather than INFO.** The WHERE-clause-list is built up via
list append, then joined with `" AND "`. If a future caller adds a new filter
that needs a derived column name (e.g. a future `segment LIKE 'markets%'` for
multi-segment intent), the natural shape would be
`where.append(f"segment LIKE '{seg_prefix}%'")` — direct interpolation. The
function as written doesn't enforce a parameter-or-literal-only contract; the
next contributor has nothing stopping them from concatenating a string.

**Recommendation.** Add a `# DO NOT format-interpolate into WHERE — bind via
params` warning above the WHERE-building block, or refactor to take only
parameterised tuples `(sql_fragment, *params)` so the contract is structural.

### MED-3 — `MAX_INLINE_RECIPIENTS` is enforced *only* by the admin route, not by the data plane

**Where:**
- `newsletter.py:563` defines `MAX_INLINE_RECIPIENTS = 500`
- `admin_routes.py` reads `db.NEWSLETTER_MAX_INLINE_RECIPIENTS` and applies the
  cap inline
- No data-plane assertion in `record_newsletter_campaign` or `create_blast_job`

**Mechanism.** The bound is a *handler-side convention*. A future caller —
a CLI tool, a sister job, a test harness, the `weekly_digest` job if anyone
ever points it at `create_blast_job` — can call
`record_newsletter_campaign(recipient_count=100_000)` followed by an inline
loop of 100k enqueue_email calls, and nothing in `queries/newsletter.py`
will protest. The bound the commit message advertises ("100k blast no longer
turns into 100k inline DB writes") only holds for *this one HTTP route*.

**Recommendation.** Either:
- Drop `MAX_INLINE_RECIPIENTS` into a check inside `record_newsletter_campaign`
  that refuses to record `sent_at = NOW()` for `recipient_count > 500` unless
  the call also supplies a corresponding `create_blast_job`, or
- Document the contract in the function docstring explicitly: "the caller is
  responsible for chunking; helpers do not enforce the cap."

The latter is cheaper; the former is safer.

### LOW-1 — Subscriber state filter is correct *for* `confirmed_at` / `unsubscribed_at`, but misses the bounce / complaint state

**Where:** `newsletter.py:455, 488, 587`

The WHERE clause is `confirmed_at IS NOT NULL AND unsubscribed_at IS NULL`.

**Status.** Strictly correct against the current schema —
`gateway/migrations/177_newsletter_segments.py` only adds `confirmed_at` and
`unsubscribed_at`. No `bounced_at`, `complained_at`, or `hard_bounce_count`
column exists.

**Gap.** The adjacent `email_jobs` pipeline does have a `bounced` status (see
`gateway/admin_emails_routes.py:65, 149, 300, 338`). A subscriber whose last 5
emails hard-bounced is still listed by `get_blast_recipients` as a valid
target. The blast then enqueues 5 more bounces on every subsequent send,
inflating bounce rates and risking sender-reputation impact at the upstream
SMTP provider.

**Why LOW.** No data-plane bug — the file correctly implements the model it
has. The gap is between the email-delivery pipeline (which knows what
bounced) and the recipient query (which doesn't subscribe to that knowledge).

**Recommendation.** Add a join (or denormalised column on
`newsletter_subscribers`) so blast recipients are filtered against
`outbound_emails WHERE status='bounced' GROUP BY recipient HAVING COUNT(*) > N`.
Or at minimum a TODO comment so the gap is visible in code.

### LOW-2 — `advance_blast_job_progress` accepts negative `batch_size`

**Where:** `newsletter.py:666-678`

```python
"UPDATE newsletter_blast_jobs SET processed_recipients = processed_recipients + ? WHERE id = ?",
(int(batch_size), int(job_id)),
```

`int(batch_size)` accepts any integer including negatives. A caller passing
`batch_size=-5` decrements progress, and could keep the job from ever flipping
to `done`. Caller today is the tick worker which passes `len(rows)`
(always ≥ 0) — but the contract is loose.

**Recommendation.** `batch_size = max(0, int(batch_size))` at function entry.

### LOW-3 — `_waitlist_position` is O(n) per call and runs on every successful subscribe / position-lookup; no upper bound on `referral_code` length

**Where:** `newsletter.py:285-313, 47-51`

- `_new_referral_code()` returns `secrets.token_urlsafe(6)[:8]`. Collision is
  ~1/10^14 per generation. The retry-on-collision loop in
  `subscribe_newsletter` (5 attempts) handles this.
- `_waitlist_position` runs three full `COUNT(*)` queries on every signup.
  Negligible at current scale (~50k subscribers — couple of MB scan), but each
  query is a full index/table scan; the audit notes this for the post-launch
  scale tracker rather than for immediate action.
- No length check on `referred_by` input — caller passes a string straight
  through to a `WHERE referral_code = ?` query, so SQLi is impossible, but a
  100kB referred_by parameter would still get touched by `strip()` and could
  bloat logs.

**Recommendation.** Cap `referred_by` to ~16 chars before the WHERE.

### LOW-4 — `mark_blast_job_failed` doesn't preserve the failure reason

**Where:** `newsletter.py:705-719`

The cron worker calls `mark_blast_job_failed(job_id)` on three different
failure modes (`campaign_missing`, `page_fetch_error`, generic exception
path) but the row only carries `status='failed'` and `finished_at`. The audit
log knows the reason; the DB row doesn't. Future support engineer triaging
`WHERE status='failed'` rows has to grep the scheduler logs.

**Recommendation.** Add a `failure_reason TEXT` column (migration 188) and
have `mark_blast_job_failed(job_id, reason)` persist it.

### INFO-1 — `_verify_confirmation_token` and `confirm_newsletter` are out of scope for this audit but use HMAC + constant-time compare correctly

`hmac.compare_digest` on the signature. Verified — not a finding, listed so
the reader knows the helper-functions section was read.

### INFO-2 — `__all__` correctly re-exports both constants

`MAX_INLINE_RECIPIENTS` and `MAX_BATCH_PER_TICK` are exported from
`__all__` and the `db.py:1018-1019` re-export aliases them as
`NEWSLETTER_MAX_INLINE_RECIPIENTS` / `NEWSLETTER_MAX_BATCH_PER_TICK`. The
naming asymmetry (un-prefixed in `queries/`, prefixed in `db`) is documented
inconsistency, not a bug.

---

## Out of scope

- SMTP-provider rate limits (the data plane is bound; upstream
  is a separate audit)
- `enqueue_email` reliability / retry contract
- The admin authentication wrapper on `/admin/newsletter/send`
- HTML escaping in `_newsletter_md_to_html` (admin-authored, but worth its own
  audit re: stored XSS on subscribers who view-as-web)
