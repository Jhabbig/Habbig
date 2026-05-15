# Audit — data-export rate-limit self-DoS verification

Targeted re-verification of the self-DoS finding from `audits/audit_data_export.md`.
**Important correction:** the earlier audit attributes the self-DoS to "HIGH-4," but the
finding is actually **HIGH-2** (`audit_data_export.md` L48). HIGH-4 is about the missing
"export ready" email. This file is the deep-dive on the rate-limit lockout risk.

**Files audited (read-only):**
- `gateway/export_routes.py` — `api_request_export` L309–327, `_run_export` L266–298
- `gateway/queries/data_exports.py` — `last_user_data_export_ts` L56–64, `create_data_export_request` L22–35
- `gateway/audits/audit_data_export.md` HIGH-2 L48 (prior finding, this is the re-verification pass)

**Severity:** **HIGH — confirmed unmitigated.** No code change has landed since the prior audit;
the self-DoS gap reproduces verbatim.

---

## Top finding

**A failed export burns the user's 24h quota — they cannot retry for ~24h, with no recovery path.**

The rate-limit check in `api_request_export` calls `db.last_user_data_export_ts(user_id)` and
compares it against `RATE_LIMIT_SECONDS = 86_400`:

```python
# export_routes.py L316–322
last_ts = db.last_user_data_export_ts(user_id)
if last_ts and int(time.time()) - int(last_ts) < RATE_LIMIT_SECONDS:
    retry_in = RATE_LIMIT_SECONDS - (int(time.time()) - int(last_ts))
    raise HTTPException(
        status_code=429,
        detail=f"Rate limit: next export available in {retry_in // 3600}h",
    )
```

And the helper it depends on:

```python
# queries/data_exports.py L56–64
def last_user_data_export_ts(user_id: int):
    """Most recent requested_at for rate-limit checking. None if never."""
    with db.conn() as c:
        row = c.execute(
            "SELECT requested_at FROM data_export_requests "
            "WHERE user_id = ? ORDER BY requested_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return int(row["requested_at"]) if row else None
```

The query does **not** filter by `status`. It returns the most recent `requested_at` from
*any* row — `pending`, `processing`, `ready`, **or `failed`**. Any row at all, regardless
of outcome.

The lockout therefore fires in every one of these scenarios — none of which the user can
self-recover from:

1. **Job crash inside `_build_zip`.** `_run_export` (L266–298) wraps the whole job in a
   broad `try/except` that sets `status='failed'` and writes the exception string to
   `error`. The row keeps its original `requested_at`. Next request: 429 for ~24h.
2. **Worker restart mid-job.** `_executor` is a 3-slot in-process `ThreadPoolExecutor`
   (L46). A gunicorn worker getting killed mid-export (deploy, OOM, SIGTERM) leaves the
   row at `status='processing'` forever — and the 24h rate-limit still applies because
   `last_user_data_export_ts` doesn't care about status. This is the same row that
   `audit_data_export.md` MED-2 calls out as orphaned.
3. **Transient DB error inside `_build_zip`.** Each section is in its own try/except so a
   single missing table doesn't crash the whole job — *but* if the outer `update_data_export_request(... status='ready')` call at L279 fails (lock contention, disk full,
   readonly FS), the broad except at L291 marks the row `failed`. Lockout again.
4. **User-correctable error.** If the failure is genuinely the user's (e.g. corrupted
   subscription row, future schema gap), they still cannot retry until the 24h clock
   elapses on the *original* request — even after support fixes the underlying data.

**Compounding UX bug — same line (L321):** the retry-in display uses integer division
`retry_in // 3600`. A user 59m59s away from re-eligibility sees `0h`, parses that as
"can retry now," retries, gets 429 again. There is no minutes/seconds resolution and no
absolute "available at" timestamp.

**Recovery surface:** none in-product. The user must email `hello@narve.ai` (the only
help address the README at `_build_zip` L189 advertises) and wait for an admin to either
delete the failed row or wait the clock out. GDPR Art. 15(3) says "without undue delay"
— a 24h forced delay because the *system* failed is plausibly non-compliant on the second
attempt, and the user has no way to know whether the first export crashed silently or is
still processing (see HIGH-4: no email).

---

## Reproduction (no code executed, derived from reading the source)

```
T0:00:00  user POSTs /api/account/export
          → last_ts is None or >24h old → passes rate-limit
          → INSERT data_export_requests(user_id=V, requested_at=T0, status='pending')
          → _executor.submit(_run_export, request_id)
          → HTTP 200 {"export_id": N, "status": "pending"}

T0:00:01  thread-pool job picks up request_id=N
          → status='processing'
          → _build_zip raises (e.g. dict iteration error, disk full, etc.)
          → except at L291 sets status='failed', error='<exc>'
          → log.exception fires

T0:00:30  user sees "FAILED" badge on /settings/privacy with red error text
          (export_routes.py L423–424). No retry button.

T0:00:31  user POSTs /api/account/export again
          → last_user_data_export_ts(V) returns T0  (status filter absent)
          → int(time.time()) - T0 = 30s < 86400s → 429
          → "Rate limit: next export available in 23h"  (or "0h" near T0+24h-ε)

T0:23:59:59  user retries
             → "Rate limit: next export available in 0h"  (integer division foot-gun)
             → still 429

T1:00:00:01  user retries
             → last_ts T0, now-T0 = 86401 > 86400 → passes
             → first chance to actually retry, 24h after a failure
```

The user spent 24h believing either their data is too large for the system (no error
detail surfaces in the UI past the truncated 80-char red text at L424) or that narve is
broken. They cannot escalate beyond the support email because no other affordance exists.

---

## Severity rationale

**HIGH, not CRITICAL.** No data leak, no auth bypass, no cross-tenant impact. The blast
radius is bounded to a single user denying themselves their own GDPR Art. 15 access for
~24h per failed attempt. It is a self-DoS, not an attacker-leveraged DoS. The reason
it stays HIGH (not MEDIUM) is the regulatory framing: a Subject Access Request that the
system silently breaks for 24h after a transient backend error is a GDPR fulfilment risk,
not just a UX paper-cut. EU regulators have fined organisations for "without undue delay"
violations as low as a few hours when the cause was a system bug the data subject couldn't
work around. Combined with HIGH-4 (no notification email) the user genuinely cannot tell
whether the system is broken or processing — they will retry, hit the lockout, and have
no recourse.

**Not exploitable by an attacker against a different user.** The rate-limit row is keyed
to `(user_id, requested_at)` and the failure is internal to that user's job. There is no
input an attacker controls that can force another user's export to fail.

---

## Fix (not applied — audit-only pass)

Two-line patch in `gateway/queries/data_exports.py`:

```python
def last_user_data_export_ts(user_id: int):
    """Most recent requested_at for rate-limit checking.

    Only counts successful or in-flight requests — failed exports should be
    immediately retryable (self-DoS otherwise; see audit_export_ratelimit.md).
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT requested_at FROM data_export_requests "
            "WHERE user_id = ? "
            "  AND status IN ('pending','processing','ready') "
            "ORDER BY requested_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return int(row["requested_at"]) if row else None
```

Display fix in `export_routes.py` L321:

```python
# Round-up so "0h" never appears for users still inside the window.
hours_left = (retry_in + 3599) // 3600
raise HTTPException(
    status_code=429,
    detail=f"Rate limit: next export available in {hours_left}h",
)
```

Belt-and-braces (still audit-only — DO NOT apply pre-release, per scope rule): consider a
worker-restart sweeper that finds `status IN ('pending','processing') AND requested_at <
now() - 600` rows on startup and marks them `failed` with `error='worker_restart'`. The
status filter above then unblocks the user automatically. (`audit_data_export.md` MED-2
already files this.)

---

## Test gap

No test in `gateway/tests/` exercises the rate-limit path for failed exports. A regression
test should:

1. Mock `_run_export` to raise immediately.
2. POST `/api/account/export` → expect 200, then poll until `status='failed'`.
3. Immediately POST `/api/account/export` again → expect 200 (post-fix) or 429 (current).
4. Bonus: assert the `detail` string never contains `" in 0h"` when `retry_in > 0`.

The closest existing coverage is `gateway/tests/test_rate_limiting.py` (modified in the
current working tree but unrelated to data-export — it tests login rate limits). There is
no `test_data_export*.py` file in the repo. Filing this as a coverage gap, not a
correctness gap.

---

## Gaps (delta vs. prior audit)

1. **Status-filter still missing in `last_user_data_export_ts`.** No commit between the
   prior audit (`audit_data_export.md`, run 2026-05-15) and HEAD touched
   `queries/data_exports.py` — verified via `git log --oneline -5` showing the latest five
   commits are all unrelated (invites/extension/social/markets test records,
   `GATEWAY_SSO_SECRET` startup enforcement). HIGH-2 is unmitigated as of HEAD
   `b1bef41`.
2. **Integer-division `0h` UX bug** at `export_routes.py:321` is unmitigated.
3. **Worker-restart orphan-row sweeper** (audit MED-2) is unmitigated — failed/stuck
   `processing` rows still occupy the rate-limit slot until manual cleanup. This compounds
   the self-DoS: even after gap (1) is fixed, a stale `processing` row still locks the
   user out because the status filter would include `processing`.
4. **No regression test** for the failed-export-retry path. `gateway/tests/` has no
   `test_data_export*.py`; the rate-limit path isn't exercised end-to-end anywhere.
5. **No in-product recovery affordance.** `/settings/privacy` renders the failed status
   with truncated red error text (L424) but no "retry now" button and no expected-retry
   timestamp. Even after the fix, a user staring at "FAILED — Rate limit: next export
   available in 23h" gets no signal that the failure cleared their quota.
6. **No audit-log entry** when an export fails (audit MED-4) — so an attacker pivoting
   into the gateway DB and forcing exports to fail (via a tampered row, or a flooded
   `_executor`) has no second source of truth showing they did so. Tangential to the
   self-DoS but it would make incident response harder if the gap were ever weaponised.

---

*Audit run 2026-05-15 against `feature/platform-build` HEAD (`b1bef41`). Re-run after any
change to `queries/data_exports.py:last_user_data_export_ts` or
`export_routes.py:api_request_export`.*
