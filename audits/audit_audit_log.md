# audit_log Coverage Audit

**Scope:** every write to `audit_log` in `gateway/**.py`. Verify: every
privileged admin action records actor / target / action / timestamp /
old-value / new-value, the `audit_log` table is never mutated from
user-facing routes, and append-only invariants hold.

**Source of truth grep:**

```
grep -rn "audit_log\|INSERT INTO audit_log\|log_audit\|audit_event" \
  gateway/ --include='*.py'
```

---

## 1. Schema & write API (the only legitimate path)

`gateway/migrations/006_security_features.py:80-102` defines:

```
CREATE TABLE IF NOT EXISTS audit_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp          INTEGER NOT NULL,
    admin_user_id      INTEGER,
    admin_email        TEXT,
    action             TEXT NOT NULL,
    target_type        TEXT,
    target_id          TEXT,
    target_description TEXT,
    before_state       TEXT,
    after_state        TEXT,
    ip_address         TEXT,
    user_agent         TEXT,
    request_id         TEXT,
    notes              TEXT
)
```

Schema captures every required field:
- **actor** → `admin_user_id` + `admin_email`
- **target** → `target_type` + `target_id` + `target_description`
- **action** → `action` (string from `AuditAction`)
- **timestamp** → `timestamp` (unix seconds, stamped server-side in
  `gateway/queries/admin.py:317` so a caller cannot forge a past time)
- **old / new value** → `before_state` + `after_state` (JSON blobs)
- **context** → `ip_address`, `user_agent`, `request_id`, `notes`

Only one INSERT path exists:

- `gateway/queries/admin.py:294-332` `insert_audit_log(...)` — the sole
  `INSERT INTO audit_log` statement in production code.

Every audit caller funnels through `gateway/security/audit.py`:

- `log_action(**kwargs)` (line 204-239) — never raises; on failure logs
  warning and returns. **Caveat:** this swallow-everything pattern hides
  bugs (see §6).
- `log_admin_action(admin_user: dict, ...)` (line 242-255) — thin wrapper
  for callers that have the current admin dict.

---

## 2. Append-only invariant

Searched for any `UPDATE audit_log` or `DELETE FROM audit_log`:

```
grep -rIn "UPDATE audit_log\|DELETE FROM audit_log\|TRUNCATE.*audit_log" gateway/
```

Hits:
- `gateway/migrations/019_remove_2fa.py:79` — one-time migration that
  scrubs two deprecated action constants (`admin.2fa_setup`,
  `admin.2fa_disable`). Runs once at DB upgrade; not reachable from any
  HTTP route. **OK.**
- `gateway/tests/test_admin_audit_log.py:63` — test fixture only.

No user-facing route mutates or deletes audit rows. The `/admin/audit-log`
page and `/admin/audit-log/export.csv` are read-only (lines
`gateway/server.py:6541-6909`); both are admin-gated and the CSV export
itself writes an `audit.csv_export` row before streaming (line 6899).

**Append-only invariant: holds in production code.**

---

## 3. Privileged actions — coverage map

### Covered (writes audit_log on success path)

| Action | Endpoint | Audit call | Captures before/after |
|---|---|---|---|
| `impersonate` (start) | `POST /admin/users/{user_id}/impersonate` (admin_routes.py:113) | line 147 → `IMPERSONATION_START`* | notes only |
| `impersonate` (end)   | `POST /admin/impersonations/end` (admin_routes.py:161) | line 173 → `IMPERSONATION_END`* | notes only |
| `impersonate` (blocked request mid-session) | implicit in middleware | server.py:1560 → `IMPERSONATION_BLOCKED`* | notes only |
| `user.promote_admin`  | `POST /admin/users/{user_id}/promote` | server.py:5792 | **yes** (snapshot_user before+after) |
| `user.demote_admin`   | `POST /admin/users/{user_id}/demote`  | server.py:5813 | **yes** |
| `user.suspend`        | `POST /admin/users/{user_id}/suspend` | server.py:5835 | **yes** |
| `user.unsuspend`      | `POST /admin/users/{user_id}/unsuspend` | server.py:5858 | **yes** |
| `user.role_change`    | `POST /admin/users/{user_id}/role` (super-admin) | server.py:6082 | **yes** + `notes=level=N` |
| `user.email_change`   | `POST /admin/users/{user_id}/email` | server.py:6119 | **yes** |
| `user.delete_completed` | `POST /admin/users/{user_id}/delete` (super-admin) | server.py:6256 | before only (after=None) |
| `user.delete_completed` | `POST /account/delete` (self) | server.py:4783 | before only |
| `user.gift_subscription` | `POST /admin/users/{user_id}/grant` (super-admin) | server.py:6198 | **no before**, after only |
| `user.trading_addon`  | `POST /admin/users/{user_id}/trading-addon` | server.py:6223 | **no before**, after only |
| `user.bulk_action`    | `POST /admin/users/bulk` | server.py:6307 | bulk after-only |
| `user.bulk_action`    | `POST /admin/users/bulk-actions` (admin_routes.py:2403) | line 2453 | bulk after-only |
| `token.generate`      | `POST /admin/tokens/generate` | server.py:5740 | after only |
| `token.generate`      | `POST /admin/users/{user_id}/new-token` | server.py:6169 | notes only |
| `token.revoke`        | `POST /admin/tokens/revoke` | server.py:5769 | none |
| `token.revoke`        | `POST /admin/users/{user_id}/revoke-token` | server.py:6142 | notes only |
| `feature_flag.create` | `POST /admin/flags` (admin_routes.py) | line 472 → `FEATURE_FLAG_CREATE`* | none |
| `feature_flag.update` | `POST /admin/flags/{key}` | line 556 → `FEATURE_FLAG_UPDATE`* | after only |
| `feature_flag.delete` | `POST /admin/flags/{key}/delete` | line 572 → `FEATURE_FLAG_DELETE`* | none |
| `email_template.update` | `POST /admin/email-templates/{key}` | line 773 → `EMAIL_TEMPLATE_UPDATE`* | none |
| `email_template.reset`  | `POST /admin/email-templates/{key}/reset` | line 801 → `EMAIL_TEMPLATE_RESET`* | none |
| `forensics.analyze`   | `POST /admin/security/forensics` (security_routes.py:341) | line 361 | after-only |
| `email.watermark_trace` | trace-watermark endpoint (admin_routes.py:869) | line 869 | notes |
| `email.delivery_resend` | `POST /admin/emails/{id}/resend` (admin_emails_routes.py:580) | line 627 | notes only |
| `audit.csv_export`    | `GET /admin/audit-log/export.csv` | server.py:6899 | notes (filters echoed) |
| `cache_clear`         | admin cache flush (admin_routes.py:1186) | line 1186 | none |
| `newsletter.blast_send` / `newsletter.blast_schedule` | admin_routes.py:2904 | line 2904 | after only |
| `api_key.create` / `api_key.revoke` / `api_key.admin_revoke` | api_keys_routes.py:268 / :303 / :401 | yes | partial |
| `webhook.create` / `webhook.delete` / `webhook.dlq.requeue` | webhooks_routes.py:240 / :259 / :419 | yes | partial |
| `admin.login` / `admin.logout` | login + logout success paths | server.py:3925 + auth flow | n/a |
| `system_alert` (job-level) | jobs/ai_jobs.py:278, jobs/claude_cost_check.py:149 | yes | n/a |

\* These action constants (`IMPERSONATION_START`, `IMPERSONATION_END`,
`IMPERSONATION_BLOCKED`, `FEATURE_FLAG_*`, `EMAIL_TEMPLATE_*`) **do not
exist in `gateway/security/audit.py:AuditAction`** — see §6.

### NOT covered (privileged action without audit_log write)

The following privileged routes mutate user/system state but do not
funnel through `_audit.log_action`. Each was confirmed by reading the
handler body and by file-level grep:

```
for f in gateway/affiliate_routes.py gateway/status_routes.py \
         gateway/take_routes.py gateway/forecast_routes.py \
         gateway/server_features.py gateway/feedback_routes.py; do
  grep -c "_audit\.log_action\|audit\.log_action\|_audit(" "$f"
done  # → all zero
```

| Endpoint | Handler | Effect | Audit? |
|---|---|---|---|
| `POST /admin/affiliates` | affiliate_routes.py:587 | creates affiliate program record | **no** |
| `PATCH /admin/affiliates/{id}` | affiliate_routes.py:640 | edits affiliate row | **no** |
| `POST /admin/affiliates/{id}/payout` | affiliate_routes.py:688 | initiates payout (money-movement intent) | **no** |
| `POST /admin/enquiries/{id}/read` | server.py:5851 | mutates enquiry state | **no** |
| `POST /admin/enquiries/{id}/create-token` | server.py:5858 | mints invite token from enquiry | **no** (the underlying `db.create_invite_token` is silent) |
| `POST /admin/incidents` | status_routes.py:493 | creates a status-page incident | **no** |
| `POST /admin/incidents/{id}` | status_routes.py:538 | edits incident | **no** |
| `POST /admin/incidents/{id}/updates` | status_routes.py:567 | posts incident updates (publicly visible) | **no** |
| `POST /admin/incidents/{id}/resolve` | status_routes.py:610 | resolves incident | **no** |
| `POST /admin/api/jobs/{name}/pause` | admin_jobs_routes.py:261 | pauses a scheduled job | **no** |
| `POST /admin/api/jobs/{name}/resume` | admin_jobs_routes.py:273 | resumes a job | **no** |
| `POST /admin/api/jobs/{name}/trigger` | admin_jobs_routes.py:285 | fires a job manually | **no** |
| `POST /admin/api/jobs/{job_id}/retry` | server_features.py:902 | retries failed job run | **no** |
| `POST /admin/jobs/weekly-digest/run` | server_features.py:876 | triggers mass email pipeline | **no** |
| `POST /admin/markets/{slug}/mark-resolved` | server_features.py:855 | marks a market resolved (affects payouts/UI) | **no** |
| `POST /admin/equivalences/{slug}/{provider}` | forecast_routes.py:453 | creates/edits market equivalences | **no** |
| `POST /admin/feedback/{id}/status` | feedback_routes.py:830 | changes feedback status | **no** |
| `POST /admin/feedback/bulk-status` | feedback_routes.py:868 | bulk status update | **no** |
| `POST /admin/feedback/{id}/duplicate` | feedback_routes.py:920 | marks duplicate | **no** |
| `POST /admin/feedback/{id}/comment` | feedback_routes.py:942 | admin posts comment | **no** |
| `POST /admin/feedback/{id}/ship` | feedback_routes.py:971 | ships a feedback item | **no** |
| `POST /api/v1/admin/takes/{id}/delete` | take_routes.py:605 | admin-deletes a user take | **no** |
| `POST /api/v1/admin/reports/{id}/resolve` | take_routes.py:622 | resolves an abuse report | **no** |
| `POST /admin/ai-cost/kill-switch` | admin_cost_alerts_routes.py:265 | super-admin **global** kill-switch toggle (billing-impact!) | **no** |
| `POST /admin/test-emails/send` | admin_test_emails_routes.py:360 | sends test email to self | **no** (no impact, low-risk gap) |
| `POST /admin/users/{id}/revoke-sessions` | admin_routes.py:2307 | force-logout user | yes, but **uses wrong action (`USER_SUSPEND`)** — see §6 |
| `GET /admin/users/{id}/export` | admin_routes.py:2350 | exports user PII | yes, but **uses wrong action (`USER_PROMOTE_ADMIN`)** — see §6 |

### Refund / plan-change

The user-instruction included `refund` and `plan-change`. There is **no
refund endpoint** in `gateway/` (`grep -rIn refund gateway/` returns only
static legal-copy mentions in `static/terms.html`, `faq.html`,
`impressum.html`). Refunds are presumably handled in Stripe directly.

Subscription / plan-change happens through:
- Stripe webhook → `gateway/stripe_webhook_routes.py` (not an admin
  action — it's a billing-system event; **not audit-logged to
  `audit_log`** but does write to its own `webhook_events` /
  `stripe_event_log` tables for replay protection).
- Admin "grant" → `POST /admin/users/{user_id}/grant`
  (server.py:6181) — **audited** as `USER_GIFT_SUBSCRIPTION`.

No "admin changes user plan / tier" endpoint exists beyond `grant` and
`trading-addon` — both **are** audited.

---

## 4. Field-level completeness check

For each covered endpoint, verified that the audit call captures:
- **actor** (admin_user_id + admin_email) — yes everywhere except the
  two system-cron writes (jobs/ai_jobs.py:278 and
  jobs/claude_cost_check.py:149) which use `admin_user_id=0`,
  `admin_email="system"` — by-design.
- **target** — yes where applicable; `target_id` is sometimes `None` for
  bulk actions (acceptable since the after-state JSON enumerates IDs).
- **action** — yes (string constant).
- **timestamp** — yes (stamped server-side in `insert_audit_log`).
- **old / new value** — **partial:**
  - User mutations (suspend/unsuspend/promote/demote/role/email)
    correctly capture `before=snapshot_user(uid)` and
    `after=snapshot_user(uid)` (`gateway/security/audit.py:135-155`,
    `_SNAPSHOT_FIELDS = (id, username, email, is_admin, suspended,
    invite_token_id, two_fa_method, deletion_requested_at, is_deleted)`).
  - `grant` / `trading-addon` / token / bulk operations record `after`
    only — there is no `before` snapshot of the prior subscription /
    addon / token state. This is a **moderate gap**: a malicious admin
    promoting then demoting back to cover tracks leaves a partial trail
    because the demote sees the wrong "before".
  - Impersonation start/end record neither before nor after.

---

## 5. Findings

### High
1. **24 privileged admin endpoints write zero audit entries.** See
   table above. Most damaging: payout creation
   (`/admin/affiliates/{id}/payout`), market resolution
   (`/admin/markets/{slug}/mark-resolved`), the global kill-switch
   (`/admin/ai-cost/kill-switch`), admin-deletes of user takes, and
   abuse-report resolutions. A compromised super-admin can move money
   and rewrite market outcomes with no forensic trail.

2. **AuditAction constants referenced but undefined.** Six action names
   used in `admin_routes.py` (`IMPERSONATION_START`,
   `IMPERSONATION_END`, `IMPERSONATION_BLOCKED`,
   `FEATURE_FLAG_CREATE/UPDATE/DELETE`, `EMAIL_TEMPLATE_UPDATE/RESET`)
   plus `SYSTEM_ALERT` in `jobs/ai_jobs.py` are **NOT** in
   `gateway/security/audit.py:AuditAction`. Verified with
   `grep -rn "IMPERSONATION_START\s*=\|FEATURE_FLAG_CREATE\s*="` →
   zero hits. At runtime each lookup raises `AttributeError`, which the
   surrounding `try/except: pass` silently swallows — the audit row is
   **never written**. Every impersonation start, every feature-flag
   change, every email-template edit silently fails to log.

### Moderate
3. **Action-string reuse to fake audit semantics.** In
   `admin_routes.py:2336` the session-revocation handler uses
   `_a.AuditAction.USER_SUSPEND` as "closest existing action"; in
   `admin_routes.py:2383` the user-PII CSV export uses
   `_a.AuditAction.USER_PROMOTE_ADMIN` as "closest neutral admin-read
   action". Both inline comments admit the misuse. This poisons the
   downstream filter on `/admin/audit-log?action=user.suspend` — a
   reviewer searching for genuine suspensions sees session-revokes
   mixed in.

4. **No `before` snapshot for grant / trading-addon / token actions.**
   The `_SNAPSHOT_FIELDS` tuple in `security/audit.py:122` lists only
   `users` table columns — subscription/addon/token mutations record
   only `after`. Reconstructing "what changed" requires correlating
   against external billing state.

5. **Audit failures are silently swallowed everywhere.** Every call
   site wraps `_audit.log_action(...)` in `try/except: pass`, and the
   function itself swallows internally. A DB-write failure to
   `audit_log` (lock timeout, disk full, schema drift) is invisible
   beyond a single `log.warning("audit.log_action failed (%s): %s")`.
   No metric, no alert. Forensic value of the table degrades silently.

### Low
6. **CSV-export endpoint logs filters, not row count.** `notes` field
   only echoes the filter dict; the actual exported row count isn't
   stored. A subpoenaed bulk-export of user emails wouldn't be
   distinguishable from a one-row diagnostic.

7. **`target_id` typed as TEXT but `admin_user_id` typed as INTEGER.**
   This is intentional — see `queries/audit.py:121` — but means joining
   `audit_log.target_id` against `users.id` requires `CAST`. Easy to
   forget when writing ad-hoc forensic queries.

8. **`/admin/test-emails/send` and `/admin/api/jobs/{job_id}/retry`
   genuinely low-risk** (test send is to self only; job retry is bounded
   by the cron schema). Listed in the table for completeness but not
   urgent fixes.

---

## 6. Append-only invariant: confirmed clean

No user-facing route in `gateway/` issues `UPDATE audit_log`,
`DELETE FROM audit_log`, or any DDL against `audit_log`. The only
mutation outside `insert_audit_log` lives in
`migrations/019_remove_2fa.py` (one-time, surgical, scoped to two
deprecated action strings) and `tests/test_admin_audit_log.py:63`
(test fixture).

The `/admin/audit-log` UI is a paginated search; the export endpoint
streams CSV with rate limiting (6 / 5 min / admin) and itself writes an
`audit.csv_export` row. No "delete row" UI, no "edit row" form.

**Conclusion:** the audit table itself is safe from tampering through
HTTP. The forensic value is degraded primarily by what **doesn't**
write to it (§5 finding 1) and by undefined action constants (§5
finding 2), not by anything that mutates after the fact.

---

## 7. Actions audited (summary count)

- **Endpoints audited correctly:** ~28 (user CRUD, super-admin
  role/email/grant, token mgmt, impersonation*, feature flags*,
  email templates*, forensics, watermark trace, audit CSV export,
  cache clear, newsletter blast, api_key + webhook lifecycle, admin
  login/logout).

  \* Six of these use undefined `AuditAction` constants and therefore
  fail silently at runtime — see §5 finding 2.

- **Endpoints NOT audited (untracked privileged actions):** 24, listed
  in the §3 second table. The most concerning are affiliate payouts,
  market-resolved, the AI-cost kill-switch, admin-delete-take,
  abuse-report resolve, status-page incident CRUD, and all admin
  feedback-tracker mutations.

- **Append-only invariant:** holds in production HTTP code.

- **Field completeness:** schema captures everything required (actor,
  target, action, timestamp, old/new value). Coverage is uneven —
  user-CRUD actions snapshot before+after; everything else captures
  after-only or notes-only.
