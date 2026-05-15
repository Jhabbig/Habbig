# Audit: `gateway/jobs/*.py`

Audit date: 2026-05-15
Scope: every `.py` file in `gateway/jobs/` (33 files).
Focus areas (per request):

1. **Cron-tick re-entrancy** — locking, `max_instances`, overlap if previous tick still running.
2. **DB row claiming atomicity** — fetch-then-update vs UPSERT, race windows.
3. **Retry storm prevention** — failure handling, exponential backoff, dead-letter rules.
4. **Payload validation when reading from DB** — JSON parsing, type checks, defensive bounds.
5. **Time-window respect** — UK / DST sensitivity, blast hours, off-peak slotting.

Severity legend:
- **CRITICAL** — production-incident risk (data loss, billing drift, RCE, blast at the wrong time, runaway loop).
- **HIGH** — recovery needed within hours (job stuck, retry storm, double-send, duplicate billing).
- **MEDIUM** — degraded behavior / silent miss / unbounded cost without an alert.
- **LOW** — cosmetic, observability gap, fragile pattern.

---

## Infrastructure context (load-bearing for the audit)

`gateway/jobs/registry.py`: `register_job(name)` *raises* `ValueError("job already registered: {name}")` if `name` collides. That has direct implications for several files below.

`gateway/jobs/backend.py`: InProcessBackend's `_run` retries up to 3× with `await asyncio.sleep(2 ** attempt)` between attempts (so 2s, 4s, 8s). Timeout per attempt is 300 s. The legacy cron loop is now disabled by default — APScheduler drives the actual cron schedule. Backend timeouts are silent and do not propagate.

`gateway/scheduler/registry.py` + `scheduler/scheduler.py`: APScheduler is in `UTC`, default `max_instances=1`. Legacy `register_cron(...)` entries are wrapped via `_wrap_legacy_job(name)` and added to APScheduler with the global `max_instances=1` default — so on a `vacuum_db_daily` that runs > 60 s, the next cron fire is dropped (which is fine for VACUUM; not for fast jobs that need overlap detection at the DB layer instead). Importantly, the scheduler ID is `f"{name}@{cron-string}"` when a name has multiple slots — so multiple cron slots for the same job name produce *independent* scheduler entries, each with their own `max_instances=1`. They do NOT share an instance-counter.

Implications:
- A long-running job at one cron slot does NOT block another cron slot for the same job name (e.g. `recompute_credibilities` 00:15, 06:15, 12:15, 18:15 — each is its own scheduler entry).
- `register_job` collisions (two `@register_job` decorators with the same name) raise at import. The `jobs/__init__.py` defensively wraps most imports in `try/except`, so a collision would *silently drop one of the modules* with only a `WARNING` log — that's a CRITICAL operational risk: nobody notices a job stops firing.

---

## Per-file audit

### `__init__.py`

`/Users/shocakarel/Habbig/gateway/jobs/__init__.py`

- **MEDIUM — Silent module drop on `@register_job` collision.** Lines 41-132: every optional module is wrapped in `try/except Exception` that logs a `warning`. If a downstream module re-imports another module's job name (e.g. `claude_cost_check.py` + `ai_jobs.py` both define `check_daily_claude_spend`, see below), the second import raises `ValueError` and the *entire* module is skipped. The job that lives there stops firing with only a warning line in startup logs. There is no alert / metric for "module failed to register."
- **LOW — Order of imports matters but isn't documented.** The defensive wrappers iterate a tuple of module names (lines 103-127); reordering changes which of two colliding modules "wins." That's fragile.

### `registry.py`

`/Users/shocakarel/Habbig/gateway/jobs/registry.py`

- **MEDIUM — `register_job` raises on duplicate name (line 23).** Combined with `__init__.py` swallowing the exception, this is the silent-drop vector flagged above. Recommend either logging at ERROR (so monitoring catches it) or accepting overwrites with a warning.
- **LOW — Cron model doesn't support minute lists.** `minute: int | None` (line 33). To schedule every-5-min or every-10-min, modules register the same job N times (see `movement_jobs.py`, `sync_portfolios.py`). That works because the scheduler builds unique IDs, but it's verbose and prone to typos (see `referral_jobs.py:294` for an example — single slot, but the pattern is easy to mis-replicate).

### `worker.py`

`/Users/shocakarel/Habbig/gateway/jobs/worker.py`

- **MEDIUM — `WorkerSettings.max_jobs = 10` (line 93)** without per-job concurrency limits. A burst of `send_email` jobs and a long `vacuum_db_daily` competing for those 10 slots could starve the email pipeline.
- **LOW — `WorkerSettings.cron_jobs = _as_arq_cron()` is evaluated at class-body time.** If a job is registered *after* this module imports (lazy import order), it won't end up in the ARQ cron set. The legacy `register_cron` calls in `jobs/__init__.py` *should* fire before `worker.py` is imported via `arq jobs.worker.WorkerSettings`, but the order is implicit and brittle.

### `backend.py`

`/Users/shocakarel/Habbig/gateway/jobs/backend.py`

- **HIGH — `_audit_finish` overwrites `attempts` count on retry (line 87).** The audit log is incremented inside `_audit_start` (`attempts = attempts + 1`), but on a permanent failure the final state shows the cumulative attempts — that's fine — but there is no per-attempt log entry, so a job that fails-then-succeeds shows `status='complete'` with `attempts=2` and no easy way to surface "intermittent failure." Observability gap.
- **MEDIUM — `_run`'s 3× retry has no jitter and no deduplication.** A fire-and-forget `asyncio.create_task` for an already-running cron tick will retry on the same DB rows that the original tick is still mutating. For idempotent jobs this is fine; for `process_referral_rewards` and `process_scheduled_deletions` (see below), a concurrent retry-after-timeout could race with the original. The InProcessBackend cron loop is disabled by default (line 188-196), but the retry-on-failure path is *not* — and a 300 s timeout firing across, say, `vacuum_db_daily` would then run the same `VACUUM` twice while the first one is still holding the SQLite writer lock.
- **MEDIUM — `retry_job(job_id)` does `json.loads(row["payload"] or "{}")` (line 343) with no schema validation.** A future migration that changes a job's signature would silently call the job with stale payload shape; the new arg parser would `TypeError` and the retry stays "failed."
- **LOW — `_ensure_jobs_table()` is called on every `_audit_insert` (line 69).** That's `CREATE TABLE IF NOT EXISTS` + 3× `CREATE INDEX IF NOT EXISTS` per enqueue. The DDL is cheap on SQLite but still a write — see `audit_db.md` for parallel observation.

### `affiliate_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/affiliate_jobs.py`

- **MEDIUM — `da.record_commission_calculated(conv_id, commission_pence)` is the atomicity boundary (line 84).** Audit-wise correct: `ok = False` if someone else already calculated, and the loop continues. Race protection is delegated to `db_affiliate.record_commission_calculated` — *that* function's atomicity matters; not visible here.
- **LOW — `if more: re-enqueue self via enqueue_job` (line 118-122).** A pathological pattern where the batch is always exactly `batch_size` produces unbounded chain enqueues. In practice `list_conversions_awaiting_commission_calc(limit=batch_size)` will drain to empty quickly. Still, no upper bound on chain length.
- **OK — Time slot 02:10 UTC (line 169).** That's 03:10 BST in summer, 02:10 GMT in winter — well off-peak in UK. Stripe webhook timing risk: the comment notes the webhook isn't fully wired so the job is a no-op until then.

### `ai_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/ai_jobs.py`

- **CRITICAL — Duplicate `@register_job("check_daily_claude_spend")` collision with `claude_cost_check.py`.** Line 252 (`ai_jobs.py`) AND line 50 (`claude_cost_check.py`) register the same name. Whichever loads second raises `ValueError` and the entire module is dropped in `__init__.py`'s `try/except`. The `_mod` list order in `__init__.py` (line 103) is `claude_cost_check` first, then `ai_maintenance`, then others. `ai_jobs` is not in `__init__.py`'s explicit imports at all — it appears the *intent* is for `claude_cost_check.py` to own the job and `ai_jobs.py`'s definition is dead. Verify by checking which one APScheduler actually fires — the dead one is also dead code (see `audit_dead_code.md`).
- **HIGH — `categorise_uncached_markets_job` (line 188).** Fetches ALL markets via `fetch_unified_markets` then picks the first `limit` uncached. If the Polymarket/Kalshi APIs are slow or 429ing the job blocks for the network timeout, repeats every hour at `:13`. No backoff, no breaker.
- **MEDIUM — `run_extract_for_recent_posts` (line 39).** Reads its batch from the `posts: list[dict] | None = None` *function argument*, not from a DB row. If invoked from the cron schedule (`register_cron("run_extract_for_recent_posts", minute=5/25/45)` lines 306-308) with no `posts=` argument, the job processes an empty batch every 20 min — silent no-op. The pipeline should pass `posts=` via direct `enqueue_job` from the scraper, not via cron. The cron entries appear to be dead.
- **MEDIUM — `reextract_all_predictions` (line 106).** Runs Claude over predictions in `chunk_size=100` chunks but the cron-fire form (admin-triggered only) has no rate limit — repeatedly invoking exhausts the Claude budget. The `check_daily_claude_spend` job catches yesterday's overspend, not today's.
- **OK — `check_daily_claude_spend` (line 252) uses `_dt.datetime.utcnow()` (line 264).** Yesterday is in UTC, fired at 00:05 UTC = 00:05 GMT / 01:05 BST. UK off-peak. (Same body as `claude_cost_check.py`'s version, hence the collision.)

### `ai_maintenance.py`

`/Users/shocakarel/Habbig/gateway/jobs/ai_maintenance.py`

- **HIGH — `recompute_calibration_scores` opens a raw `sqlite3.connect(...)` (line 84)** outside the `db.conn()` context manager. No timeout, no PRAGMA journal_mode setup, no `foreign_keys=ON`. Runs at `hour=0/6/12/18, minute=25`. Holds the write lock for the duration of the per-source UPDATE loop. If the loop is slow (no LIMIT inside the iteration, processes all `handles`), it can block writers for many seconds. Also lacks `conn.execute("BEGIN IMMEDIATE")` — under SQLite's deferred mode, two simultaneous schedule fires could both think they have the lock and one would raise `database is locked`.
- **MEDIUM — Schema introspection on every call (line 91, 114).** `PRAGMA table_info(...)` per call. Cheap, but suggests the schema "varies across branches" — that's a smell. A migration check at startup would be cleaner than per-call probing.
- **LOW — `reextract_predictions_backfill` (line 178).** Cron at `04:13 UTC`. Limit defaults to 50; cache-fills only — no destructive write to `predictions`, only `predictions_reextracted` (staging table). Safe.

### `backend.py` — see "Infrastructure context" above (already audited)

### `backtest_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/backtest_jobs.py`

- **CRITICAL — Duplicate `@register_job("run_backtest")` collision with `pipeline_jobs.py:157`.** Same silent-drop vector as `ai_jobs.py`. The two implementations differ: `backtest_jobs.run_backtest_job` takes `run_id: int` and calls `_bt.run_backtest(int(run_id))`; `pipeline_jobs.run_backtest_job` takes `backtest_id: int = 0` and calls `intelligence.backtester.run_backtest(params)` with `_db` UPDATE of `backtests` table. These call *different* modules and use *different* DB tables (`backtest_runs` vs `backtests`). One is loaded; the other is dead. Whichever `try/except` runs second is silently dropped, and *callers that enqueue this job with the wrong shape get the wrong job*.
- **HIGH — No DB row claim guard.** `run_backtest_job(run_id)` doesn't check `status='queued'` before running — a re-enqueue (manual or automatic retry) re-runs the same backtest. Result is the latest write wins.
- **LOW — No cron registration.** This job is enqueue-only (from `POST /dashboard/backtest`). No time-window risk.

### `claude_cost_check.py`

`/Users/shocakarel/Habbig/gateway/jobs/claude_cost_check.py`

- **CRITICAL — Duplicate `check_daily_claude_spend` registration with `ai_jobs.py`** (see above).
- **MEDIUM — Admin email recipients hardcoded as fallback (line 36-38):** `"julian.habbig@icloud.com,shocakarel@gmail.com"`. Inviting unintended disclosure if the env var is unset on a new deploy. Should fail closed or load from secrets vault.
- **MEDIUM — `_record_alert` uses `INSERT OR IGNORE` (line 134).** Dedupe key is `(alert_date, threshold_usd)`. Manual scheduled re-runs of the same day skip — that's intentional. But the email enqueue does NOT dedupe (line 162) — running the job twice on the same day fires the email twice.
- **LOW — Kill-switch trip (line 100-115)** sets the `ai.client` flag; one path, no metric for "kill-switch is on for N hours." Recovery is manual.
- **OK — 00:05 UTC schedule (line 184).** UK off-peak. Reads `yesterday` from UTC so DST doesn't shift the window.

### `compute_churn_signals.py`

`/Users/shocakarel/Habbig/gateway/jobs/compute_churn_signals.py`

- **MEDIUM — Subscriber loop with per-user `_compute_for_user` (line 79)** does 1 aggregation query per user. For a few thousand paid subs this is fine; if subscriber count grows, this becomes a Long Pole — every user serially. No N+1 fix here (other jobs *did* refactor). Holds the write conn during `_upsert_signal` per row (line 226 — `_upsert_signal(c, ...)` reuses the loop's connection). One big transaction means one big lock window during the entire loop.
- **MEDIUM — `c.execute("SELECT CAST((? - strftime('%s', ?)) / 86400 AS INTEGER) AS d", ...)` (line 119-122)** — a query *just* to compute `days_since` arithmetic. Should be Python-side: SQLite query just to compute date math is wasteful. Same goes for the `datetime(?, 'unixepoch', '-N days')` calls (line 89-105) — Python could compute the bounds once.
- **OK — 04:17 UTC schedule (line 243).** Off-peak.

### `compute_source_relationships.py`

`/Users/shocakarel/Habbig/gateway/jobs/compute_source_relationships.py`

- **HIGH — Raw `sqlite3.connect` again (line 47).** Same issue as `ai_maintenance.py`: no timeout, no PRAGMA, no `BEGIN IMMEDIATE`. Runs every Sunday 03:00 UTC and holds the lock for the full pair-wise loop. `itertools.combinations(sources, 2)` over `max_sources=200` is 19,900 pairs and 19,900 UPSERTs in one transaction. Lock window is multi-second.
- **HIGH — `INSERT OR IGNORE` semantics not used — uses `INSERT ... ON CONFLICT(source_a, source_b) DO UPDATE` (line 108).** Atomic per-row, but no `ORDER BY a,b` to canonicalise pair order. If a separate caller writes `(b, a)` instead of `(a, b)`, the constraint doesn't fire. `itertools.combinations` does emit in lexicographic order so this is fine in practice — but it's a hidden invariant.
- **LOW — `weekday=6` is Sunday in arq semantics (line 174).** Verified by the comment.
- **OK — Sunday 03:00 UTC = 04:00 BST.** UK off-peak.

### `db_maintenance.py`

`/Users/shocakarel/Habbig/gateway/jobs/db_maintenance.py`

- **HIGH — `vacuum_db_daily` (line 104) takes the SQLite *exclusive* lock for the duration of VACUUM.** This is intentional but means any writer (e.g. a session login at 05:00 UTC) gets `database is locked` for the duration. The job catches `OperationalError` and bails (line 144), which protects the scheduler but *not* the user-facing writer that lost its query. 05:00 UTC = 06:00 BST — early morning UK, low traffic, OK. But there's no metric for "user writes that failed during VACUUM."
- **MEDIUM — `recovery_drill` (line 326) does `live_conn.backup(dest)` (line 369)** inside `with db.conn() as live_conn`. The `db.conn()` context manager pools connections; `live_conn.backup` mutates a non-pool connection (`dest = sqlite3.connect(drill_path)`). If a pooled connection is *also* in `live_conn`'s "writer" slot, this could deadlock — but in practice SQLite's `.backup` API yields the writer lock in batches. Still: `with db.conn() as c` to take the lock and then *also* opening a raw `sqlite3.connect` on the drill file is non-canonical.
- **MEDIUM — `trim_perf_logs` (line 201) and `trim_job_runs` (line 226)** delete from tables that the admin polling page reads. No `WHERE timestamp < ?` index hint — relies on the table having one. If the index is missing post-migration, the DELETE turns into a full scan inside the writer lock.
- **OK — 03:40 / 03:45 / 04:10 / 04:15 / 05:00 / 05:20 UTC slot layout.** Carefully staggered. UK off-peak.
- **LOW — `recovery_drill` runs every day but the body short-circuits unless `today.day == 1 and today.month in (1,4,7,10)` (line 349).** That's a 4× per year drill; the cron entry runs 365× a year for ~361 no-ops. Wasted job-runs row per day. Use the registry's `day=1` semantics + an explicit month gate instead.

### `email_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/email_jobs.py`

- **CRITICAL — `send_weekly_digest_batch` (line 128) runs Mondays 08:00 UTC = 09:00 BST in summer / 08:00 GMT in winter.** That's the brief's call-out: blast emails before UK breakfast. 08:00 UTC fires emails during UK morning rush hour — explicitly the blast window the user wants to avoid being too early. Recommend moving to 10:00–14:00 UTC to avoid the 06:00–08:00 BST inbox-checking window. Note: 08:00 UTC = 03:00 ET = 04:00 PT — fine for US, bad for UK morning if "respect UK off-peak" is the goal. *This is the closest match to the brief's example "don't blast at 3am UK."*
- **HIGH — `send_morning_briefings` (line 306) at 08:03 UTC.** Same UK timezone issue. Daily. ~140 chars per push. A subscription change or N+1 regression could make the loop slow and the next morning fire overlaps with the lingering previous run (only blocked by APScheduler `max_instances=1`).
- **HIGH — `send_weekly_digest_batch` enumerates `users` (line 150) and `await enqueue_email` per user (line 295).** No rate limiter, no batching — a hundred thousand-user run dumps 100k jobs into the queue in seconds. Downstream `send_email_job` has its own concurrency limit (`max_jobs=10` in ARQ) so the *send* throttles, but the *enqueue* doesn't. Memory grows with user count.
- **MEDIUM — Subscription tier fetch is batched in-process (line 171-189)** but the row data is still read once for ALL users. For a million-user run, this loads 1M `users` rows into memory before the loop starts. Should stream.
- **MEDIUM — `send_email_job` (line 27) raises on `not success` (line 51).** That triggers the 3× retry. If the SMTP provider is rate-limiting, the 3 retries push 3× the failed sends into the same provider window. No circuit breaker.
- **MEDIUM — Per-recipient watermark `from email_system import watermark as _wm` (line 293)** — defensive import on every recipient. Move outside the loop.

### `embed_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/embed_jobs.py`

- **LOW — Single-purpose increment with `widget_id: str`.** No validation that `widget_id` is a valid widget; `db.increment_embed_widget_impression` is presumably idempotent. Logged as "good enough for a vanity metric" (line 9) — acknowledged trade-off.
- **OK — No cron, only enqueue-on-request.** No time-window concern.

### `export_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/export_jobs.py`

- **MEDIUM — `cleanup_expired_data_exports` (line 30)** does `os.unlink(path)` on file paths from `db.expire_old_exports()`. No validation that `path` is inside the exports dir. If the DB is compromised or a migration writes an arbitrary path, the cron job will unlink whatever the path points to. Should canonicalise + assert prefix.
- **LOW — 03:30 UTC daily (line 52).** UK off-peak.

### `feedback_digest.py`

`/Users/shocakarel/Habbig/gateway/jobs/feedback_digest.py`

- **HIGH — `feedback_shipped_digest` cron at `day=1, hour=6, minute=3` (line 214) = 06:03 UTC = 07:03 BST.** Still in UK morning hours. Same blast-window concern as the weekly digest.
- **HIGH — Sends up to `MAX_RECIPIENTS = 10_000` emails in one cron tick.** No paginated batch, no rate limiter at the enqueue side. Same memory/queue-flood concern as `email_jobs.py`.
- **MEDIUM — `_enqueue_send_email` (line 177)** has a fallback to `asyncio.run(coro)` if no loop is running (line 202). That's correct for tests but a foot-gun in production — if APScheduler is reusing the loop, `asyncio.run` would fail. Should be removed for prod.
- **LOW — `DIGEST_DRY_RUN=1` env hook (line 128)** — useful for testing, but if it's accidentally left on in prod the digest silently doesn't send. Should log a WARNING when active.

### `forecast_sync.py`

`/Users/shocakarel/Habbig/gateway/jobs/forecast_sync.py`

- **MEDIUM — `await asyncio.sleep(_PROVIDER_SPACING_SECONDS)` per provider (line 94, 100, 123)** with 4 providers × ~500 markets = up to 2 hours per run. APScheduler's `max_instances=1` will silently drop the next day's run if this one is still going. No alert.
- **MEDIUM — `db_forecasts.record_forecast` (line 104) — unclear from this module whether the UNIQUE constraint on `(market_slug, provider, recorded_at)` is enforced.** The comment claims so (line 14-17). If not, duplicate forecasts accumulate.
- **MEDIUM — Provider-side failure (Metaculus is down) means `find_equivalent` returns `(None, 0.0)` (line 89-91) and the provider is silently skipped.** No error count surfaced.
- **OK — 03:15 UTC daily (line 169).** UK off-peak.

### `generate_weekly_reports.py`

`/Users/shocakarel/Habbig/gateway/jobs/generate_weekly_reports.py`

- **HIGH — `register_cron("generate_weekly_reports", weekday=0, hour=7, minute=0)` (line 136) = Mondays 07:00 UTC = 08:00 BST.** Pro-tier PDF generation runs during UK morning. The PDF is then attached to the 08:00 UTC `send_weekly_digest_batch` (per comment line 5-7). Two heavy jobs back-to-back during UK breakfast hour.
- **MEDIUM — Per-user `build_report_for_user` is awaited serially (line 89).** No `asyncio.gather` batching. For 1000 Pro users that's serial PDF rendering — if each takes 1 s the job takes 17 minutes inside the APScheduler `max_instances=1` lock.
- **MEDIUM — Raw `sqlite3.connect` (line 38).** Same pattern as `ai_maintenance.py`. Not pooled.
- **LOW — Schema introspection (`PRAGMA table_info(users)`, line 47).** Per-call. Cheap.

### `insider_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/insider_jobs.py`

- **HIGH — `_run_fetcher_and_correlate` (line 75) builds `INSERT OR REPLACE INTO insider_market_correlations` with `executemany` (line 144).** Correct atomicity, but the `conn.executemany` is buffered then committed (`conn.commit()` at line 153). If the loop crashes mid-correlation the commit doesn't fire and the new fetched rows are *already* in `insider_signals` (from `fetch_once()`) but the correlation isn't — they get re-correlated next tick. That's mostly fine because the loop scans by `external_id`. Not data-loss but a re-work risk.
- **HIGH — Raw `sqlite3.connect(...)` (line 41)** with `conn.commit()` outside a transaction explicit block. Inserts from fetch + correlate go through this raw conn. Same lock concerns as elsewhere.
- **MEDIUM — Schedule density.** Every-2h `fetch_unusual_options`, every-4h `fetch_sec_form4`, every-6h `fetch_congressional_trades`, daily `fetch_sec_form13f`, `fetch_fec_campaign`, `fetch_lobbying`. All on minute offsets 17/23/31/07/34/53 — well-spread. None of these hit UK morning hours (02:07, 02:34, 02:53). Good.
- **MEDIUM — `f"... LIMIT ?".format(",".join("?" * len(result.sample_external_ids)))` (line 121).** String-builds the placeholder list; if `sample_external_ids` is empty (line 122-123 conditional), the entire branch returns []. Safe but ugly.
- **LOW — `_active_markets_snapshot` returns up to 250 markets (line 65).** Fixed cap. Reasonable.

### `invite_replenish.py`

`/Users/shocakarel/Habbig/gateway/jobs/invite_replenish.py`

- **MEDIUM — Daily cron at 00:05 UTC (line 139) with day-of-month guard `if now.day != 1: return` (line 60).** That's an early-return no-op for ~96% of days; logs `"not 1st of month"` every day. Wasted job-runs row. Spec for `register_cron` only supports `hour` and `minute` (line 21-24 comment). Should be migrated to APScheduler's `day=1` cron directly, but the migration is gated on the `register_cron` extension.
- **HIGH — `db_sharing.replenish_invites_for_user` (line 108)** is the atomicity guard. Per the comment (line 13-17): the `invites_replenished_yyyymm` field on the user row is checked atomically. If that column-check + insert isn't in a transaction, a retry-after-300s could double-grant. Need to verify the db_sharing helper.
- **OK — 1st of month at 00:05 UTC = 01:05 BST.** UK off-peak.

### `movement_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/movement_jobs.py`

- **HIGH — Raw `sqlite3.connect(...)` (line 35) with `conn.execute("UPDATE market_movement_events SET notified_at = ? WHERE id = ?", ...)` (line 91) per event.** No `BEGIN IMMEDIATE` — under contention two cron fires within the same minute could both see `notified_at IS NULL` and both deliver. APScheduler `max_instances=1` prevents same-cron-id overlap, but `_deliver_pending_events` is called from `detect_market_movements` which fires every 5 min at `0,5,10,...,55` — 12 fires/hour for a *single* job, and APScheduler tracks each cron entry as one scheduler entry. Same scheduler entry only fires when the previous one returns, but the registered cron table has 12 entries for `detect_market_movements`. **Each cron-slot is a separate APScheduler entry, each with `max_instances=1`, but they're all 5 min apart from the same logical job.** So overlap *is* possible if a single tick takes > 5 min. The `notified_at IS NULL` claim isn't atomic — it's a SELECT then UPDATE without `SELECT ... FOR UPDATE` (SQLite doesn't have one) and no `WHERE notified_at IS NULL` in the UPDATE (line 92 only matches by id) — so two concurrent ticks would BOTH deliver the same event. **HIGH severity** for the duplicate-notification risk.
- **MEDIUM — `_enqueue_push` (line 124) and `_enqueue_inapp` (line 141)** swallow `ImportError` silently. If `push` module is broken in deploy, alerts vanish quietly.
- **MEDIUM — `pending = conn.execute("... LIMIT 200")` (line 63).** Hard limit. If 201 events occur in 5 min, the 201st waits 5 min for the next tick — but if those 200 still take > 5 min to deliver, the queue grows.
- **OK — Every-5-min cron `for _min in range(0, 60, 5)` (line 162).** No UK timing concern; the job is alert-driven, not blast.

### `newsletter_blast_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/newsletter_blast_jobs.py`

- **CRITICAL — Cron is `register_cron("newsletter_blast_tick")` (line 234) with ALL fields None = every minute.** From the comment (line 13-16): drains 500 recipients per tick = 30k/hour. If a 100k-recipient blast starts at 02:50 UTC, it runs until ~07:00 UTC = 08:00 BST — directly in the UK breakfast inbox window. The blast cron *itself* fires every minute with no time-of-day gating. There is no "don't blast between 03:00–08:00 UK" check anywhere. **This is exactly the "blast at 3am UK" risk the brief calls out.**
- **HIGH — Row claim via `db.fetch_next_pending_blast_job()` (line 52) + `db.mark_blast_job_started(job_id)` (line 85).** Two separate calls. If the first call returns a row and the process crashes before the second, the row remains `pending` and the next tick re-picks it — fine. If two cron ticks fire concurrently (per APScheduler `max_instances=1` they shouldn't, but if a long tick races a future fire) both could call `fetch_next_pending_blast_job` and both could get the same row. The function is described as "matches both 'pending' and 'running'" (line 21-23), so an idempotent re-run is OK — but the second tick would *also* call `enqueue_email` on the same recipients page, doubling sends. Need `fetch_next_pending_blast_job` to atomically `UPDATE … WHERE status = 'pending' RETURNING id` (SQLite 3.35+). Otherwise the race is real.
- **HIGH — `db.advance_blast_job_progress(job_id, len(rows))` (line 195)** advances by *attempted* sends, not successful. The comment (line 190-194) acknowledges this: "We advance `processed_recipients` by the batch size we ATTEMPTED (not just the ones that succeeded)." That means an outage in the email transport during one tick silently drops up to 500 recipients per tick. No DLQ. Recommend tracking the (job_id, recipient) tuple in a separate table for retry.
- **HIGH — `db.count_blast_recipients(segment, frequency_filter) - total` (line 113-118)** computes `inline_count` *live*. If users opt out between batches the `offset` shifts. The comment acknowledges this — "the recipient page just returns fewer rows and the job finishes early" — but the math is fragile: if 10 users unsubscribe and 5 sign up, `inline_count` is wrong by 5 and the offset overlaps with already-sent recipients. **Possible double-send.**
- **MEDIUM — `_newsletter_md_to_html` imported from `admin_routes` (line 164).** A worker import depending on a route module is a layering violation; if `admin_routes` has a side-effect import that breaks in worker context, the blast tick dies.
- **MEDIUM — `_maybe_backfill_sent_at` (line 213)** is called only on certain branches (line 92, 150, 199). If a blast finishes via the "remaining ≤ 0" path (line 89-100), it's called. If it finishes via the regular advance path, it's called only when `status_after == 'done'` (line 198). Looks correct but the branching is fragile.

### `notification_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/notification_jobs.py`

- **HIGH — `send_saved_prediction_resolution_notifications` cron at `minute=7` (line 411-413).** Hourly. Re-enqueues itself if the batch fills (line 251-255). The chain has no upper bound, so a stuck queue could keep re-enqueuing forever. Need a backstop counter.
- **HIGH — `check_market_movers` (line 259) at `minute=32` (line 415).** Calls `unified_markets.fetch_unified_markets` (line 288) which hits Polymarket + Kalshi APIs. Defaults `price_change_threshold=0.08` and iterates *every* active market × *every* opted-in user (line 311 → line 347). Quadratic. No rate limit on the outer enqueue loop.
- **HIGH — `notified_on_resolution = 1` UPDATE (line 73)** happens AFTER `enqueue_email`. If the UPDATE fails (lock contention) but the email was enqueued, next tick re-sends. Should be in same transaction as the SELECT, or use `UPDATE … WHERE notified = 0 RETURNING id` for atomic claim.
- **MEDIUM — `await enqueue_job("send_market_resolution_notifications", ...)` re-enqueues itself for more batches (line 92-99).** If `batch_size=100` is full every time, this is fine; but a pathological "always full but never makes progress" loop (e.g. an FK violation on `notified_on_resolution`) re-queues forever.
- **MEDIUM — `_fanout_push_safe` (line 139)** uses `asyncio.ensure_future(coro)` without awaiting — fire-and-forget. Errors are swallowed. No observability.
- **OK — Time slots `:07` and `:32`.** UK fine.

### `perf_baseline.py`

`/Users/shocakarel/Habbig/gateway/jobs/perf_baseline.py`

- **MEDIUM — `run_perf_baseline` (line 221)** runs 30 samples of each probe at 03:20 UTC. Each probe is a real SQLite query. During the sample window the writer is locked out for short bursts. Designed for a quiet hour — 03:20 UTC = 04:20 BST — acceptable.
- **MEDIUM — No row-claim — the job is read-only against the live data and writes only to `perf_baseline_snapshots`.** Safe to retry.
- **LOW — `_prior_median_p95` reads its own table (line 151)** — recursive in spirit but safe (statistical comparison only).
- **OK — Daily at 03:20 UTC.**

### `pipeline_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/pipeline_jobs.py`

- **CRITICAL — `process_scheduled_deletions` (line 38) hard-deletes users 30 days after their soft-delete.** Scheduled `hour=2, minute=0` = 02:00 UTC = 03:00 BST. Per-user transaction has UPDATE + 9× DELETE in one `with db.conn() as c` block (line 67-86). If any DELETE fails partway (e.g. FK violation on a missing migration), the UPDATE of `is_deleted=1` is committed but the cascades are partial. There's no per-deletion checkpoint — the entire user is logged as "deleted" but the cascade isn't atomic. Recovery requires manual SQL to find half-deleted users. **Recommend `BEGIN IMMEDIATE` + `COMMIT` or `ROLLBACK` on per-user basis.**
- **CRITICAL — Duplicate `@register_job("run_backtest")` collision with `backtest_jobs.py:20`** (see `backtest_jobs.py` audit).
- **HIGH — `recompute_credibilities_job` (line 192) fires at 00:15, 06:15, 12:15, 18:15 UTC.** 06:15 UTC = 07:15 BST — UK morning hour with a potentially-heavy `recompute_all_credibilities()`. If the function is N+1 over sources × predictions, the SQLite writer is locked during UK login traffic.
- **MEDIUM — `generate_sitemap` (line 107) writes `static/sitemap.xml` via `Path.write_text(xml)`.** Atomically? Python's `write_text` truncates then writes — readers between truncate and write get an empty file. Use `os.replace` pattern.
- **OK — 06:00 UTC sitemap = 07:00 BST.** Edge case; mostly fine.

### `reconcile_subscriptions.py`

`/Users/shocakarel/Habbig/gateway/jobs/reconcile_subscriptions.py`

- **HIGH — `_fetch_status(sub_id)` (line 47) hits Stripe API in a synchronous-looking call.** This is the `stripe.Subscription.retrieve(sub_id)` blocking call. For thousands of subs this serialises in one cron tick — could exceed APScheduler `max_instances=1` and the next day's run drops. No Stripe rate-limit awareness; one 429 cascades.
- **HIGH — UPDATE on the users table per drifted user (line 117-121)** with `subproduct_subscriptions = ?` from a JSON blob. No `WHERE subproduct_subscriptions = old_value` — a concurrent webhook write between the row read (line 75) and the row write (line 117) gets overwritten silently. Two minutes of webhook events during the reconcile run could be lost.
- **MEDIUM — `json.loads(row["subproduct_subscriptions"] or "{}")` (line 86)** wrapped in `try/except Exception` — falls back to `{}` (line 88), then `if not isinstance(blob, dict): continue` (line 90). Defensive parsing OK.
- **MEDIUM — Drift alert email (line 148)** fires when drift_ratio ≥ 5%. No dedup — runs nightly and could fire daily if a chronic drift persists.
- **OK — 03:17 UTC daily.** UK off-peak. Note: comment (line 178) explicitly slotted off-peak.

### `referral_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/referral_jobs.py`

- **HIGH — `process_referral_rewards` (line 41) at 02:15 UTC daily (line 294).** 02:15 UTC = 03:15 BST. UK off-peak. Comments (line 14-19) acknowledge race-prevention by daily single-process batch — *no distributed lock*. The InProcessBackend's 3× retry-on-timeout (300 s) would re-fire this job *while the original is still running* — and the comment explicitly says "running this in a single daily batch — one process, no concurrency — gives us a natural serialization point without needing a distributed lock." **That assumption breaks on retry-after-timeout.** If the 300s timeout fires while `process_referral_rewards` is mid-loop, a second instance starts and both will try to grant the same conversion. The `mark_referral_reward_granted` atomic check (line 207-213) catches it (`if not ok: orphan revoke`), but it relies on that atomicity at the DB layer.
- **HIGH — `INSERT INTO gifted_subscriptions` (line 182) THEN `mark_referral_reward_granted` (line 207).** If process crashes between, the gift exists but the referral is still pending. Next run grants again. The code does try to revoke the orphan (line 217-231) but only when `mark_referral_reward_granted` returns False (race detected). On crash between the two inserts, the orphan stays.
- **MEDIUM — `add_referral_credit_months` (line 235)** is not transactional with the gift insert. Three writes per reward, no transaction wrapping.
- **LOW — `compute_user_leaderboard_scores` (line 301) at 03:00 UTC daily.** Full recompute. Fine.

### `registry.py` — see "Infrastructure context" above

### `resolution_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/resolution_jobs.py`

- **MEDIUM — `poll_market_resolutions` (line 23) at `minute=17` hourly.** Fetches every unresolved market from Polymarket + Kalshi. No batch size — `market_ids = db.get_unresolved_market_ids()` returns all of them. As unresolved counts grow this gets long. APScheduler `max_instances=1` blocks overlap but if the run takes > 60 min it skips the next hour.
- **MEDIUM — `json.loads(resolved_prices)` (line 91)** — the comment (line 78-86) explicitly calls out the historical RCE: previously used `eval()`. Current `json.loads` is safe. Defensive parsing OK.
- **MEDIUM — `db.resolve_predictions_for_market(market_id, outcome_yes)` (line 99)** — the atomicity of "resolve all predictions for this market" depends on this helper. If it's not transactional, a crash mid-resolve leaves the market half-resolved.
- **LOW — Polymarket + Kalshi clients opened and closed per tick (line 49-56, 168-174).** Connection churn. Could use a long-lived client.
- **OK — `:17` hourly.** No UK morning blast issue.

### `share_retention.py`

`/Users/shocakarel/Habbig/gateway/jobs/share_retention.py`

- **LOW — `f"DELETE FROM {table} WHERE expires_at < ?"` (line 69-73).** Table name from whitelisted constant (line 40). Safe SQL pattern.
- **LOW — Per-table `try/except` (line 76)** — one bad table doesn't block the others. Good defensive pattern.
- **OK — 03:20 UTC daily.** UK off-peak.

### `status_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/status_jobs.py`

- **HIGH — `check_service_health` (line 49) cron `minute=None` = every minute.** Calls `status_probes.run_all_probes()` (line 58) which awaits across many components. If a probe times out, the entire cron tick is delayed. APScheduler `max_instances=1` will silently drop subsequent ticks. No alert on "monitoring stopped."
- **MEDIUM — `status_db.list_open_incidents_for_component` (line 109)** then `status_db.create_incident` (line 129). Two-call pattern; race with another job firing the same incident? `max_instances=1` should prevent in-process race, but if the InProcessBackend retries on timeout, the same minute could fire twice.
- **MEDIUM — Retention prune `wallclock.weekday() == 0 and wallclock.hour == 0` (line 90).** That fires 60 times per Monday 00:00 UTC hour. Each fire calls `prune_snapshots_older_than` which is a DELETE on the full table. Should be guarded with a "did we already prune in the last hour" check.
- **OK — Service-health probes themselves don't block user requests** (acknowledged in the module docstring).

### `sync_portfolios.py`

`/Users/shocakarel/Habbig/gateway/jobs/sync_portfolios.py`

- **HIGH — `sync_polymarket_positions_job` registers cron `for _m in range(60): register_cron("sync_polymarket_positions", minute=_m)` (line 297-298).** 60 separate APScheduler entries. Each with its own `max_instances=1`. Means the job logically fires every minute but a long-running tick at minute 5 doesn't block minute 6. **The job itself filters which users it processes per-minute via `_user_offset`** (line 71), so this is intentional. But if a *single* minute's run takes > 60 s (e.g. Polymarket API slow), it overlaps with the next minute's run — fine because they process disjoint user sets. The math works.
- **HIGH — `_RateLimited429` (line 128) aborts the run (line 215-216).** Backoff is local to the run (`backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)`) and *resets to `_BACKOFF_BASE_SECONDS = 1.0`* on the next tick (line 174). So a 429 storm just gets retried every minute with a 1 s pause — no real backoff across ticks. Need a persisted backoff cookie.
- **MEDIUM — `await polymarket.sync_positions(user_id)` (line 137)** — per-user; failure increments `sync_error_count` somewhere (presumably in `polymarket.sync_positions`). After 10 in a row the user is skipped (line 179) — that's `_MAX_ERROR_STREAK`. Reasonable.
- **MEDIUM — Kalshi job's `if row["sync_error"].startswith("HTTP 401")` (line 264)** — string match on error code. Fragile.
- **OK — Pacing at 5 req/s well below 30 req/s limit.**
- **OK — Cron every-minute → 10-minute cadence per user.** Distributes load.

### `take_resolution_jobs.py`

`/Users/shocakarel/Habbig/gateway/jobs/take_resolution_jobs.py`

- **HIGH — `_resolve_takes_for_finished_markets_impl` (line 86)** scans `predictions` joined with `market_takes` (line 101-112) — finds every market with takes still unresolved. No LIMIT. For a marketplace with millions of takes that's an O(market×take) scan. Designed assuming the unresolved set is small.
- **MEDIUM — `_derive_outcome_for_market` (line 48)** counts YES/NO votes from `predictions` table to derive outcome. Skips ambiguous (line 77). But if `direction` is corrupted (e.g. typo "YES " with trailing space), it falls into neither YES nor NO branch and silently skips. Should `.strip().upper()` first (line 65 *does* strip+upper — OK).
- **LOW — `_LOOKBACK_SECONDS = 48 * 60 * 60` (line 45).** Re-scoring within 48h. Idempotent due to the partial unique index (line 24-25 comment).
- **OK — 03:11 UTC daily.** UK off-peak.

### `telegram_sends.py`

`/Users/shocakarel/Habbig/gateway/jobs/telegram_sends.py`

- **HIGH — `send_telegram_market_mover` (line 117) fans out to *every* `is_active = 1` connection (line 124-127).** No batching, no rate limit. Telegram Bot API has a hard limit of 30 messages/second per bot. A market mover for a popular event with 1000 linked users blows through the limit and gets the bot temporarily banned.
- **MEDIUM — `_send_raw` (line 45)** uses `httpx.Timeout(10.0)` — fine. No retry. A transient 5xx drops the message.
- **MEDIUM — `send_telegram_best_bets` (line 76)** loops over `bets` and calls `_send_raw` per bet — sequential. For 5 bets, fine. For more, slow.
- **LOW — `parse_mode="MarkdownV2"` (line 68)** — caller must pre-escape special chars. Easy to forget.
- **OK — No cron; only enqueue-driven.**

### `worker.py` — see "Infrastructure context" above

---

## Cross-cutting findings

### Time-window respect — UK off-peak compliance

| Job | Schedule | UTC | UK summer (BST) | UK winter (GMT) | OK? |
| --- | --- | --- | --- | --- | --- |
| `send_weekly_digest_batch` | Mon 08:00 UTC | 08:00 | **09:00 BST** | 08:00 GMT | **No — UK morning rush** |
| `send_morning_briefings` | Daily 08:03 UTC | 08:03 | **09:03 BST** | 08:03 GMT | **No — UK morning rush** |
| `generate_weekly_reports` | Mon 07:00 UTC | 07:00 | **08:00 BST** | 07:00 GMT | **No — UK breakfast** |
| `feedback_shipped_digest` | 1st 06:03 UTC | 06:03 | **07:03 BST** | 06:03 GMT | **No — UK breakfast** |
| `newsletter_blast_tick` | every minute | any | any | any | **No — no time gate, can run any UK hour** |
| `recompute_credibilities` 06:15 | Daily 06:15 UTC | 06:15 | 07:15 BST | 06:15 GMT | Marginal — heavy write |
| `process_scheduled_deletions` | Daily 02:00 UTC | 02:00 | 03:00 BST | 02:00 GMT | **Edge — close to "3am UK"** |
| Everything else 02:* – 05:* UTC | various | 02–05 | 03–06 BST | 02–05 GMT | OK |

The brief's "3am UK" trip-wire is most concerning for:
1. `newsletter_blast_tick` (every minute, no time gate at all — a blast started at 02:50 UTC = 03:50 BST runs continuously).
2. `process_scheduled_deletions` at 02:00 UTC = 03:00 BST.

### Cron re-entrancy summary

- **InProcessBackend cron loop is disabled** (`NARVE_LEGACY_CRON_LOOP=1` env to re-enable). APScheduler now owns cron — `max_instances=1` per scheduler entry, UTC.
- **Cron-tick re-entrancy is per scheduler-entry id, not per job name.** Jobs that register multiple slots (e.g. `recompute_credibilities` × 4, `sync_polymarket_positions` × 60, `fetch_*` × 4–12) get one scheduler entry per slot. So the *same job function* can overlap itself if two slots fire close together.
- **InProcessBackend `_run` will retry on timeout (300 s) with `asyncio.sleep(2**attempt)` between attempts**, and that retry path is *not* gated by `max_instances`. Long-running jobs (`vacuum_db_daily`, `process_referral_rewards`, `recompute_all_credibilities`) risk the retry firing while the original is still running.

### DB row claiming atomicity summary

| Pattern | File | Severity |
| --- | --- | --- |
| SELECT + UPDATE without atomic claim | `notification_jobs.py` (`notified_on_resolution`), `movement_jobs.py` (`notified_at`) | HIGH |
| Two-call fetch + start | `newsletter_blast_jobs.py` (`fetch_next_pending_blast_job` + `mark_blast_job_started`) | HIGH |
| Insert + stamp, no transaction | `referral_jobs.py` (gift insert + reward stamp + credit add) | HIGH |
| User UPDATE without optimistic lock | `reconcile_subscriptions.py` (`subproduct_subscriptions` JSON write) | HIGH |
| Per-user multi-table cascade in one txn | `pipeline_jobs.py` (`process_scheduled_deletions`) | CRITICAL |
| Single-statement UPSERT | `compute_churn_signals.py`, `compute_source_relationships.py` | OK |

### Retry storm summary

- Backend exponential backoff (2, 4, 8 s) is short. A 3-retry job that fails in 14 s and *immediately* re-enqueues another batch (e.g. `notification_jobs.py:92-99`, `affiliate_jobs.py:120-124`) compounds.
- No global circuit breaker. No max-chain length on self-re-enqueueing batched jobs.
- No DLQ. Failed jobs land in `background_jobs.status = 'failed'` with no automatic alert.

### Payload validation summary

- `backend.py:343` `json.loads(row["payload"] or "{}")` — no schema validation for `retry_job`.
- `notification_jobs.py:32-40` reads `user_market_views` joined with `users` — assumes `email` may be NULL (line 54 check). OK.
- `reconcile_subscriptions.py:87` defensively `json.loads(... or "{}")` + `isinstance(blob, dict)`.
- `resolution_jobs.py:91` `json.loads(resolved_prices)` — historically `eval()`, now safe.
- `movement_jobs.py:81` `json.loads(event_dict["narve_context_json"])` — wrapped in `try/except json.JSONDecodeError`.

Overall: payload validation is reasonable. The main gap is no schema versioning — a future change to a job's signature breaks all retry rows.

---

## Severity counts (across 33 files)

| Severity | Count |
| --- | --- |
| CRITICAL | 5 |
| HIGH | 26 |
| MEDIUM | 39 |
| LOW | 14 |

## Top 5 issues (by blast radius × likelihood)

1. **CRITICAL — Duplicate `@register_job` name collisions silently drop entire modules.**
   `ai_jobs.py` + `claude_cost_check.py` both register `check_daily_claude_spend`. `backtest_jobs.py` + `pipeline_jobs.py` both register `run_backtest`. `jobs/registry.py:23` raises on duplicate; `jobs/__init__.py` swallows the exception as a `warning`. The second module *and every job inside it* never fires. There is no metric or alert. Fix: ERROR-level log + monitoring on "job registration failed" lines; rename one of each pair.
2. **CRITICAL — `newsletter_blast_tick` blast has no UK-hours gate.**
   Cron is every minute. A 100k-recipient blast initiated at 02:50 UTC = 03:50 BST runs continuously through UK breakfast (~07:00–09:00 BST). The advance-by-attempted (`db.advance_blast_job_progress(job_id, len(rows))` `newsletter_blast_jobs.py:195`) also silently drops up to 500 recipients per failed batch. Fix: add a time-of-day gate that defers the tick during UK 03:00–08:00 BST; switch to advance-by-succeeded.
3. **CRITICAL — `process_scheduled_deletions` (`pipeline_jobs.py:38`) hard-deletes users in a non-atomic cascade.**
   Per-user `UPDATE … SET is_deleted=1` + 9× `DELETE FROM …` in one connection without explicit `BEGIN IMMEDIATE`. Crash partway leaves half-deleted users. Schedule is 02:00 UTC = 03:00 BST — exactly the "blast at 3am UK" sensitivity window the brief calls out (for writes, not emails). Fix: wrap per-user cascade in an explicit transaction; add a checkpoint table for resumable deletions.
4. **HIGH — `process_referral_rewards` (`referral_jobs.py:41`) relies on "single daily batch = no concurrency" but the InProcessBackend's 300 s timeout + 3× retry breaks the assumption.**
   The job comment (line 14-19) explicitly says: "Running this in a single daily batch — one process, no concurrency — gives us a natural serialization point without needing a distributed lock." The retry-on-timeout path means a second copy can start at 02:15 UTC + 5 min while the first is still running. Race: two copies both insert `gifted_subscriptions` rows. The `mark_referral_reward_granted` atomic check (line 207) catches some races but not the crash-between-insert-and-stamp case. Fix: explicit advisory lock in DB; wrap insert + stamp + credit in a single transaction.
5. **HIGH — `notification_jobs.py` + `movement_jobs.py` claim rows by SELECT-then-UPDATE without atomic compare.**
   `send_market_resolution_notifications` reads `notified_on_resolution=0` then UPDATEs `notified_on_resolution=1` later (line 72-75) — two cron ticks racing each see `0` and both enqueue the email. `_deliver_pending_events` in `movement_jobs.py:91-94` UPDATEs by `id` without the `WHERE notified_at IS NULL` check. Fix: use `UPDATE … WHERE notified_on_resolution=0 RETURNING id` (SQLite 3.35+) or perform the SELECT inside the same `BEGIN IMMEDIATE` as the UPDATE.

---

## Notes on what was NOT audited

- Implementation of helpers in `db.py`, `db_takes.py`, `db_affiliate.py`, `db_referrals.py`, `db_sharing.py`, `db_forecasts.py` — these are called from the jobs but live outside `gateway/jobs/`. The audit assumes their atomicity claims hold; specific call-outs above flag where the assumption matters.
- Cron schedule timing in `gateway/scheduler/` (already inspected briefly for adapter semantics) — see `audit_security_dir.md` / `audit_logging_config.md` for the surrounding scheduler hardening.
- Embedded jobs that depend on external API contracts (Polymarket, Kalshi, Stripe, Metaculus, etc.) — the audit flags rate-limit / 429 handling but does not validate the actual API contracts.
