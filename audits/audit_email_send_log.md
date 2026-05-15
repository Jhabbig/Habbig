# Audit — `email_send_log` writes & retention

**Date:** 2026-05-15
**Scope:** Verify (a) what code writes to `email_send_log`, (b) what code reads
from it, (c) whether retention is bounded so the table can serve compliance
(GDPR Art. 15 evidence of communications sent) without growing forever.
**Method:** synchronous bash only. Pre-release pages not touched. Followed
the brief's hard rules.

---

## TL;DR — retention gap

**There is no retention gap because there is no table.** `email_send_log`
does not exist anywhere in the schema:

- No `CREATE TABLE email_send_log` in any of the 108 files under
  `gateway/migrations/`.
- Not present in `gateway/db.py::SCHEMA` (lines 22-275).
- Zero `INSERT`/`UPDATE` statements anywhere in the tree
  (`grep -rln "INSERT INTO email_send_log" gateway/` returns nothing).

The **single reference** in the entire repo is a `SELECT` in dead code:
`gateway/exports/generator.py:347-354`. It is wrapped in `_safe_query`,
which catches `OperationalError: no such table` and silently returns `[]`
(see the helper docstring at `generator.py:157-180`).

So the compliance posture is the **inverse** of what the brief asks to
verify — emails are not "logged for compliance but not indefinitely";
they are **not logged in `email_send_log` at all**, and the GDPR
notifications/history section in the user export ZIP is silently empty
on every run.

The actual de-facto record of outbound email is the
`background_jobs` table (`name = 'send_email'` rows), which **also has
no retention sweep wired** — so retention there is *infinite*, the
opposite gap.

---

## 1. Where `email_send_log` is referenced

Single hit:

```
gateway/exports/generator.py:347-354
    # Notification history — sent emails for this user, best-effort.
    # The `email_send_log` and `saved_predictions.notified_on_resolution`
    # tables are the closest thing we have to "notifications sent".
    bundle["notifications"] = _safe_query(
        c,
        "SELECT * FROM email_send_log WHERE user_id = ? "
        "ORDER BY sent_at DESC LIMIT 1000",
        (user_id,),
    )
```

This is in `gateway/exports/generator.py`, which is the dead-code
GDPR export module per `audits/audit_data_export.md:5` (only `tests/`
import from it; the production export path is the truncated
`_build_zip` in `gateway/export_routes.py`). Even when this dead
module is hit by the test suite, `_safe_query` swallows the
`no such table` error and returns `[]`, so nothing ever surfaces.

---

## 2. Writes — none

```
$ grep -rln "INSERT INTO email_send_log\|UPDATE email_send_log" gateway/
(no output)
```

`gateway/email_system/service.py` is the single send dispatcher. It
hands rendered messages to SMTP / Postmark / DRY_RUN — no DB write
on success or failure. The send-side audit log is `log.info` and
`log.warning` only.

The job-queue side does record activity: `enqueue_email` (defined at
`gateway/jobs/email_jobs.py:55-71`) calls `enqueue_job("send_email", ...)`,
which delegates to `gateway/jobs/backend.py::_audit_insert`
(lines 68-76) writing into `background_jobs(name='send_email',
payload=<json>, status=…, enqueued_at, started_at, finished_at,
duration_ms, error)`. That is the **only** persistent record of
"we sent email X to recipient Y" anywhere in the system — and the
admin /admin/emails page (`gateway/admin_emails_routes.py:137-211`)
treats it as such.

---

## 3. Retention — none on either side

### 3.1 `email_send_log` — table does not exist

- No schema → no rows → no retention required. The compliance
  property "logged but not indefinitely" is **not satisfied**; the
  weaker property "not logged at all" is satisfied by accident.

### 3.2 `background_jobs` (the de-facto send log) — unbounded

`gateway/jobs/backend.py:40-65` creates the table with
`enqueued_at INTEGER NOT NULL` and three indexes. No corresponding
`DELETE`/`trim`/`purge` exists in production code:

```
$ grep -rln "DELETE FROM background_jobs" gateway/ | grep -v tests/
(no output)

$ grep -n "background_jobs" gateway/jobs/db_maintenance.py
189:# old retry rows in background_jobs) should still run — point them at
```

The `db_maintenance` module's `trim_*` family covers
`slow_request_log`, `slow_query_log`, `job_runs`, and
`wallet_connect_nonces`; `background_jobs` is **not** swept (see
`audits/audit_log_retention.md` for the broader retention map). The
only `DELETE FROM background_jobs WHERE name = 'send_email'` calls
live in test fixtures:

- `gateway/tests/test_admin_emails.py:129, 275`
- `gateway/tests/test_weekly_digest.py:40`

In production, every `send_email` job — including the full
`payload` JSON containing **the recipient email address, template
name, and full template context** (which can include unsubscribe
URLs, watermark IDs, weekly-digest content snippets, morning-
briefing market data) — accumulates forever in `background_jobs`.

---

## 4. The retention gap — summary

| Layer                                 | Writes?        | Retention?           | Effect on compliance                                                                                                       |
|---------------------------------------|----------------|----------------------|----------------------------------------------------------------------------------------------------------------------------|
| `email_send_log` (named table)        | **No**         | n/a (no rows)        | GDPR Art. 15 export bundle ships `notifications: []` — controller cannot show the subject what communications it sent.       |
| `background_jobs.name='send_email'`   | **Yes** (every enqueue) | **None — unbounded** | Every recipient + payload persists forever. Inverse compliance problem: data-minimisation (Art. 5(1)(c)) and storage-limitation (Art. 5(1)(e)) both implicated. |

So the brief's premise (emails logged for compliance, retention TBD)
is **doubly wrong**: the named log doesn't exist, and the de-facto
log is never bounded.

---

## 5. Fix paths (pick one)

**Option A — drop the dead reference, document the gap.** Remove the
`email_send_log` SELECT from `gateway/exports/generator.py:347-354`,
update the dead-code README inside that module, and either (a) point
the GDPR export bundle at `background_jobs` filtered by
`name='send_email'` + recipient match (extracting `to` from
`payload` JSON), with a 90-day retention layered onto
`background_jobs` via a new `trim_send_email_jobs` cron in
`jobs/db_maintenance.py`, or (b) accept the gap and document in the
ZIP README that "transactional email history is retained on the
delivery provider, not the controller."

**Option B — build the table properly.** Add a migration
`gateway/migrations/189_email_send_log.py` creating
`email_send_log(id, user_id, recipient, template, sent_at, status,
error, provider_message_id)` with an index on `(user_id, sent_at)`.
Wire `email_system/service.py::EmailService.send_template` to
INSERT one row per send attempt. Add `trim_email_send_log(days=90)`
to `jobs/db_maintenance.py` next to `trim_perf_logs`, on the same
03:40 UTC slot. Wire the GDPR export (the real one in
`gateway/export_routes.py::_build_zip`, **not** the dead generator)
to read from this table.

**Option C — minimal compliance.** Keep the storage in
`background_jobs` (the existing de-facto log), but add the trim:
`DELETE FROM background_jobs WHERE name = 'send_email' AND
finished_at IS NOT NULL AND finished_at < (now - 90 days)`. Also
add a `name='send_email'`-aware path in the GDPR export.

Option A is the least invasive. Option B is the cleanest. Option C
plugs the unbounded-growth half of the problem fastest, leaving the
GDPR-export half for a follow-up.

---

## 6. Hard-rule compliance

- **Synchronous bash only.** Every `Bash` invocation in this audit
  was a foreground command; no `run_in_background`.
- **Pre-release pages off-limits.** This audit reviewed
  `gateway/jobs/`, `gateway/exports/`, `gateway/migrations/`, and
  `gateway/admin_emails_routes.py`. None of those files are
  pre-release surfaces.
- **No code changes.** Read-only audit; output is this file only.
