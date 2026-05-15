# Log retention audit — 2026-05-15

**Scope:** retention / rotation / cleanup cron for the five log-style
tables called out in the brief: `audit_log`, `security_events`,
`slow_request_log`, `slow_query_log`, `email_send_log`.

**Method:**
1. Locate the `CREATE TABLE` (migrations) for each.
2. Grep `gateway/` for any `DELETE FROM <table>`, `trim_*`, `purge_*`,
   or retention helper.
3. Cross-reference against `register_cron(...)` in `gateway/jobs/` so a
   trim helper without a schedule is treated as "no retention".
4. Pre-release pages were not touched. Only synchronous bash was used.

---

## 1. Per-table summary

| Table              | Schema source                              | Trim function                                                | Cron schedule (UTC)            | Window  | Status         |
|--------------------|--------------------------------------------|--------------------------------------------------------------|--------------------------------|---------|----------------|
| `audit_log`        | `migrations/006_security_features.py:82`   | none (append-only by design)                                  | none                           | forever | NO RETENTION   |
| `security_events`  | `migrations/072_security_events.py:23`     | none                                                          | none                           | forever | NO RETENTION   |
| `slow_request_log` | `migrations/096_slow_request_log.py:27`    | `jobs/db_maintenance.py::trim_perf_logs` (line 201-223)       | `register_cron(..., hour=3, minute=40)` (line 308) | 30 days | covered        |
| `slow_query_log`   | `migrations/081_slow_query_log.py:37`      | `jobs/db_maintenance.py::trim_perf_logs` (line 201-223) — also a standalone helper at `queries/performance.py::trim_slow_query_log` (line 167-175), unused by cron | `register_cron("trim_perf_logs", hour=3, minute=40)` | 30 days | covered        |
| `email_send_log`   | **NOT CREATED in any migration**           | n/a                                                           | n/a                            | n/a     | GHOST TABLE    |

The `trim_perf_logs` job handles both performance tables in a single
nightly pass at 03:40 UTC. The helper at
`queries/performance.py:167-175` (`trim_slow_query_log`) is a leftover
public API documented in the module docstring at line 25 but is **not
wired to any cron** — the live retention path is the
`jobs/db_maintenance.py` job.

---

## 2. Tables without retention

### 2.1 `audit_log` — append-only by design

- **Schema:** `gateway/migrations/006_security_features.py:80-102`.
- **Writes:** sole `INSERT INTO audit_log` is
  `gateway/queries/admin.py:294-332::insert_audit_log`. Funnelled
  through `gateway/security/audit.py::log_action` / `log_admin_action`.
- **Deletes:** the only `DELETE FROM audit_log` in the entire tree is a
  one-shot migration that scrubs two deprecated 2FA action constants
  (`gateway/migrations/019_remove_2fa.py:79`). Plus the test fixture in
  `tests/test_admin_audit_log.py:63`. No production path mutates rows.
- **Append-only invariant** holds per the earlier coverage audit
  (`audits/audit_audit_log.md` §2 and §6). This is intentional: an
  audit trail you can rotate is not an audit trail.
- **Action required:** none — flagged here for completeness because the
  brief asked for "tables without retention". A future GDPR or
  forensics decision (e.g. pseudonymisation after N years) could be
  layered on top without breaking the append-only contract, but is
  out of scope for this pass.

### 2.2 `security_events` — no retention, growth unbounded

- **Schema:** `gateway/migrations/072_security_events.py:23-46`.
- **Writes:** populated from `POST /api/security/capture-attempt` and
  any future client-/server-side detector (see migration docstring).
  Indexes are tuned for `(user_id, created_at)` and `(event_type,
  created_at)` reads.
- **Deletes:** zero. No `DELETE FROM security_events` anywhere in
  `gateway/` outside the table's own `downgrade()`.
- **Risk:** the migration says "high-volume usage (>5 events per user
  per 10 min) triggers an admin alert at read time (no scheduled
  worker)". With no retention, every alarm event persists forever.
  Cost is bounded today (single-digit rows/day) but a screen-share
  campaign or anti-forensic abuse spike could push the table into the
  hundreds of thousands of rows, and the `(event_type, created_at)`
  index inflates the WAL on every burst write.
- **Suggested follow-up:** add a `trim_security_events(days=90)` to
  `jobs/db_maintenance.py` and a `register_cron` slot adjacent to the
  03:40 `trim_perf_logs` window. 90 days is the conventional retention
  for low-volume security telemetry — long enough for incident
  forensics, short enough to keep the table bounded.

### 2.3 `email_send_log` — GHOST TABLE

- **Schema:** none. No `CREATE TABLE email_send_log` exists in
  `gateway/migrations/` or anywhere else in the tree.
- **Only reference:** `gateway/exports/generator.py:347-354` performs
  `SELECT * FROM email_send_log WHERE user_id = ? ORDER BY sent_at`
  inside `_safe_query`, which silently returns `[]` when the table
  doesn't exist (`_safe_query` is documented at line 157-180 as
  swallowing `OperationalError: no such table`).
- **Effect:** every GDPR export currently emits `notifications: []`
  with a manifest warning. No email-send activity is captured anywhere
  — `email_system/service.py` sends via SMTP / DRY_RUN / relay without
  logging to a row.
- **Status:** retention is moot until the table exists. Whoever
  introduced the export reference (commit log not chased here)
  intended a send-log table that never landed. Either (a) drop the
  dead `SELECT` from the export bundle and document the gap, or
  (b) add a migration creating `email_send_log` and wire
  `EmailService` to write per-send rows, then layer a 90-day
  `trim_email_send_log` on the same nightly slot as
  `trim_perf_logs`.

---

## 3. Covered tables — confirmation

### 3.1 `slow_request_log`

- Schema: `migrations/096_slow_request_log.py:27` — docstring at line
  8 explicitly delegates retention to `jobs.db_maintenance`.
- Trim: `jobs/db_maintenance.py:201-223` — deletes
  `WHERE timestamp < (now - 30 days)`. Errors swallowed per row so a
  single missing table doesn't take down the other trim.
- Cron: `register_cron("trim_perf_logs", hour=3, minute=40)` at line
  308. Slotted before the 04:10 WAL checkpoint so the deletes land in
  the same nightly checkpoint cycle.

### 3.2 `slow_query_log`

- Schema: `migrations/081_slow_query_log.py:37` — docstring at line 8
  says retention is via `queries.performance.trim_slow_query_log`, but
  the **actual cron path** is the unified `trim_perf_logs` job above.
  Both functions exist; only the `jobs/db_maintenance.py` one is
  scheduled.
- Tracer-side write path:
  `queries/query_tracer.SlowQueryTracer` is fire-and-forget so a slow
  write here can never stall a user request (see migration 081
  docstring).
- **Doc drift note (low severity):** the migration 081 docstring
  points readers at the unused helper. Either delete
  `trim_slow_query_log` in `queries/performance.py` or update the
  migration docstring to reference `jobs.db_maintenance.trim_perf_logs`
  as the live cron path.

---

## 4. Summary

- **Tables without retention:** `audit_log` (by design),
  `security_events` (gap — recommend 90 d), `email_send_log` (does
  not exist).
- **Tables with retention covered:** `slow_request_log` (30 d via
  `trim_perf_logs` at 03:40 UTC), `slow_query_log` (same job).
- **Hard rule compliance:** synchronous bash only; pre-release pages
  not touched.
