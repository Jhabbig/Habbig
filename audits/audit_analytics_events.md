# Audit — `analytics_events` table growth

**Date:** 2026-05-15
**Scope:** `analytics_events` table in `gateway/auth.db` — row count, on-disk
footprint, and whether any retention / rotation policy bounds growth.
**Verdict:** **NO RETENTION POLICY.** The table is append-only and grows
unbounded except for per-user cascades on account deletion. Live row
count is tiny today (10), so this is a latent issue, not an active fire.

---

## Live DB measurements

Run against `/Users/shocakarel/Habbig/gateway/auth.db` (SQLite 3.51.0,
page size 4096):

| Metric                        | Value                          |
|-------------------------------|--------------------------------|
| Row count                     | **10**                         |
| Oldest `created_at` (UTC)     | 2026-04-21 15:10:25            |
| Newest `created_at` (UTC)     | 2026-04-21 15:18:45            |
| Time span covered             | ~8 min (single seeding session)|
| Table data pages              | 1 page = **4096 B** (`dbstat`) |
| `idx_analytics_type` size     | 4096 B                         |
| `idx_analytics_created` size  | 4096 B                         |
| `idx_analytics_user` size     | 4096 B                         |
| Total `analytics_events` cost | **16 KiB** (1 table + 3 idx)   |
| Whole `auth.db` file size     | 2,482,176 B (≈ 2.37 MiB)       |

Event-type breakdown (current 10 rows):

| event_type          | n |
|---------------------|---|
| `newsletter_signup` | 6 |
| `feed_view`         | 2 |
| `page_view`         | 2 |

Average column lengths (rough projection input):

- `properties`  ≈ 2 bytes (mostly `{}`)
- `ip_hash`     ≈ 4.4 bytes (test fixtures — real SHA256 would be 64)
- `session_id`  NULL on every current row

The local dev DB is essentially empty — it reflects a single seed
session, not real production traffic. The interesting question is
therefore **what bounds growth in prod**, not what's on disk today.

---

## Schema

`gateway/db.py:374-389`:

```sql
CREATE TABLE IF NOT EXISTS analytics_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type           TEXT NOT NULL,
    user_id              INTEGER REFERENCES users(id) ON DELETE SET NULL,
    session_id           TEXT,
    page                 TEXT,
    referrer             TEXT,
    ip_hash              TEXT NOT NULL,
    user_agent_category  TEXT,
    properties           TEXT,
    created_at           INTEGER NOT NULL
);
CREATE INDEX idx_analytics_type    ON analytics_events(event_type);
CREATE INDEX idx_analytics_created ON analytics_events(created_at);
CREATE INDEX idx_analytics_user    ON analytics_events(user_id);
```

The `idx_analytics_created` index is exactly what a retention sweep
would need (`WHERE created_at < cutoff`) — but no sweep exists.

---

## Retention / rotation status — **NONE**

### Evidence

1. **No DELETE statements anywhere.**
   `grep -rn "DELETE FROM analytics_events" gateway/` returns **zero**
   matches. The only writes are the single INSERT in
   `gateway/queries/admin.py:121-136` and per-user cascades when a user
   row is deleted (`user_id REFERENCES users(id) ON DELETE SET NULL` plus
   the explicit per-table DELETE that the hard-delete cron performs).

2. **`db_maintenance.py` skips it.**
   `gateway/jobs/db_maintenance.py` is the canonical home for retention
   trims. It registers four `register_job(...)` entries:
   - `wal_checkpoint` (04:10 UTC)
   - `vacuum_db_daily` (05:00 UTC)
   - `trim_perf_logs` (`slow_request_log`, `slow_query_log` — 30 d)
   - `trim_job_runs` (`job_runs` — 30 d)
   - `trim_wallet_connect_nonces` (1 h)
   - `recovery_drill` (quarterly)

   `analytics_events` is **not** in this list. There is no
   `trim_analytics_events` job, no cron registration that touches it.

3. **GDPR audit comments explicitly call out retention.**
   `audits/audit_gift_sub.md:239` and `audits/audit_gdpr.md:174`
   document that `analytics_events` is **deliberately retained** when
   a user is deleted ("subscriptions, analytics_events, user_bet_history
   retained — financial / behavioural ledger"). That decision is about
   **what to do at account-deletion time**, not a recurring sweep —
   the table still has no time-based cap.

4. **The insert path adds rows on every analytics event with no cap.**
   `queries/admin.py:121-136` is a plain INSERT, no rotation around it,
   no caller-side cap. Any page view, feed view, newsletter signup,
   gate entry, etc. adds a row that lives until the user account is
   purged (and even then only their rows go, not the table's tail).

### What this means at production scale

Back-of-envelope with realistic columns: typical row ≈ 200 B + index
overhead → ≈ 400 B / row on disk after indexes. Estimating event
volume:

| Daily events | Daily growth | 1 yr             | 3 yr             |
|--------------|--------------|------------------|------------------|
| 1 k          | ~400 KB      | ~146 MB          | ~440 MB          |
| 10 k         | ~4 MB        | ~1.5 GB          | ~4.4 GB          |
| 100 k        | ~40 MB       | ~15 GB           | ~44 GB           |

The 3-index footprint means every row pays the cost ~3×. At 10 k
events/day (modest for a SaaS that logs page views), `analytics_events`
alone passes the *entire current* `auth.db` file size inside a month
and starts dominating VACUUM cost and `.backup` snapshot time within a
year.

---

## Findings

### 1. [HIGH] No time-based retention sweep — table grows unbounded
**Location:** `gateway/jobs/db_maintenance.py` (missing job),
table defined at `gateway/db.py:374-389`.

The other high-write logs in the same DB (`slow_request_log`,
`slow_query_log`, `job_runs`) are all swept on a 30-day window.
`analytics_events` writes at a comparable or higher rate (every page
view, feed view, signup, gate entry) and is not swept at all. The
file-size, VACUUM-duration, and `.backup` window all degrade as
this table grows. There's no business reason that page-view rows
from 2024 still need to be on the live OLTP DB in 2026 —
admin/prerelease analytics queries in `queries/admin.py:140-180`
all filter `WHERE created_at >= ?` against a recent cutoff, so
ancient rows aren't even read on the hot path.

**Recommendation:** add a `trim_analytics_events(days: int = 180)`
job to `jobs/db_maintenance.py` modeled on `trim_perf_logs`:

```python
@register_job("trim_analytics_events")
async def trim_analytics_events(days: int = 180) -> dict[str, Any]:
    import db
    cutoff = int(time.time()) - int(days) * 86400
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM analytics_events WHERE created_at < ?",
            (cutoff,),
        )
        removed = cur.rowcount or 0
    return {"ok": True, "removed": removed, "cutoff_ts": cutoff}

register_cron("trim_analytics_events", hour=3, minute=50)
```

The 03:50 UTC slot sits between `trim_wallet_connect_nonces`
(03:45) and `wal_checkpoint` (04:10), so deletes land in the same
nightly checkpoint cycle. `idx_analytics_created` already covers
the cutoff predicate, so the sweep is O(rows_deleted) not O(table).

Pick `days` by policy (180 d is a reasonable default — covers
half-yearly cohort comparisons without retaining indefinitely).
If aggregate metrics must survive trimming, add a parallel
`analytics_daily_rollup` table that the sweep populates before
deleting.

### 2. [LOW] `idx_analytics_user` is redundant if no per-user reporting query exists
**Location:** `gateway/db.py:389`

Quick grep didn't find a hot-path `WHERE user_id = ?` query against
`analytics_events`. The index is paid for on every insert. If the
only use is the GDPR per-user export (`audits/audit_gdpr.md`),
that runs once per export and could tolerate a table scan on a
trimmed table. Not urgent — flagged for cleanup once retention
lands.

### 3. [INFO] Insert path is privacy-clean
**Location:** `gateway/queries/admin.py:111-137`,
`gateway/server.py:417, 5044`.

`ip_hash` is salted (`IP_HASH_SALT` — see also
`ENV_DEFAULTS_AUDIT.md` HIGH #2 about not hardcoding the salt) and
the table holds no raw IP, no email, no name. So retention is a
size/cost problem, not a regulatory one — but trimming still
reduces blast radius of an exfiltrated DB dump.

---

## Bottom line

- **Row count:** 10 (dev DB; one ~8-min seed window 2026-04-21).
- **Footprint:** 16 KiB total (4 KiB data + 12 KiB indexes); whole
  `auth.db` is 2.37 MiB.
- **Retention:** **NONE.** No `trim_analytics_events` job, no DELETE
  statements outside per-user account-deletion cascades. Indefinite
  growth in production. Add a 180-day sweep to
  `jobs/db_maintenance.py` (one job, one cron line, indexed scan).
