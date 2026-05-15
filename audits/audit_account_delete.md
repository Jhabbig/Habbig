# Adversarial Audit — Account Deletion Flow

**Date:** 2026-05-15
**Auditor:** Claude (Opus 4.7)
**Targets:**

- `/Users/shocakarel/Habbig/gateway/server.py` — synchronous form handler `POST /account/delete` (lines 4725–4823, route registered at 4725)
- `/Users/shocakarel/Habbig/gateway/server_features.py` — JSON API `POST /api/account/delete` (lines 531–584) and `POST /api/account/delete/cancel` (lines 587–602)
- `/Users/shocakarel/Habbig/gateway/queries/auth.py` — `cascade_delete_user` (lines 870–902), `revoke_all_user_sessions` (lines 934–943), `delete_sessions_for_user` (lines 120–127)
- `/Users/shocakarel/Habbig/gateway/jobs/pipeline_jobs.py` — cron `process_scheduled_deletions` (lines 38–104), registered daily at 02:00 UTC (line 215)
- `/Users/shocakarel/Habbig/gateway/migrations/005_account_deletion.py` — schema for `deletion_requested_at`, `deletion_scheduled_for`, `deletion_cancelled_at`, `is_deleted`, `deleted_at`
- `/Users/shocakarel/Habbig/gateway/migrations/122_market_takes.py` line 23 — `market_takes.user_id ON DELETE SET NULL`
- `/Users/shocakarel/Habbig/gateway/db.py` — `sessions` (lines 36–44), `user_bet_history` (lines 139–153), `PRAGMA foreign_keys = ON` (line 261)
- `/Users/shocakarel/Habbig/gateway/static/privacy.html` line 411 — Privacy Policy refers to `POST /account/delete`
- `/Users/shocakarel/Habbig/gateway/security/audit.py` — `USER_DELETE_*` audit-log action constants

**Scope requested:** confirmation requirement (CSRF + password re-entry), soft-delete grace period, hard-delete cascade across all FK references, Stripe customer deletion called, session revocation on delete.

No code changes were made (per task hard rule).

---

## 0. Result summary

| Severity | Count |
|----------|------:|
| Critical | 1 |
| High     | 4 |
| Medium   | 4 |
| Low      | 3 |
| Info     | 2 |
| **Total**| **14** |

### Top 3 findings (ranked by exploitability × impact)

1. **CRIT-1 — Two divergent deletion flows live side-by-side.** `POST /account/delete` (form, `server.py:4725`) does an **immediate hard-delete via `cascade_delete_user`** and wipes the user row. `POST /api/account/delete` (`server_features.py:531`) does a **30-day soft-delete** (timestamps only) handed off to the daily `process_scheduled_deletions` cron. The privacy page at `static/privacy.html:411` documents the form route, so a GDPR Art.17 request honoured through the documented surface produces an *irrecoverable* delete with **no grace period**, violating the user-facing recovery contract that the soft-delete API was built to provide and removing the user's ability to revoke an accidental click. See CRIT-1.
2. **HIGH-1 — Stripe customer / subscription is never told the account is gone.** Neither flow calls `stripe.Subscription.cancel()` nor `stripe.Customer.delete()` — both only flip `subscriptions.status = 'cancelled'` in SQLite (`server_features.py:560–562`, `billing_routes.py:921–924`). The Stripe-side subscription keeps billing the saved card; the next renewal silently creates a Stripe webhook event (`customer.subscription.updated`) targeting a user row that no longer exists (form path) or whose email is anonymised to `deleted_<id>@deleted.narve.ai` (soft path). Customers continue to be charged for a deleted account, and `stripe_webhook_hardening.py:262` joins on `stripe_customer_id` so the webhook silently no-ops. See HIGH-1.
3. **HIGH-2 — `cascade_delete_user` ignores `ON DELETE SET NULL` and wipes data the schema intended to retain.** `queries/auth.py:870–902` enumerates every table with a `user_id` column and issues `DELETE FROM <table> WHERE user_id = ?`. Tables that deliberately declare `ON DELETE SET NULL` (notably `market_takes` line 23, `discord_*` link tables line 36, `feedback_submissions` line 62 of `130_feedback.py`, `share_metrics` line 46, `security_events` line 26) are bulk-deleted instead of having their `user_id` nulled. This contradicts the schema author's intent (preserve research/forensic rows after account closure) and silently destroys data the soft-delete path takes care to retain (the pipeline job explicitly skips these — `pipeline_jobs.py:88` comment). See HIGH-2.

---

## 1. Inventory of routes and helpers

`grep -rn "delete_account\|account_delete\|cancel_account\|is_deleted\|deletion_requested" gateway/ --include='*.py'` was the entry point; relevant matches grouped by role:

| Surface | File:line | Behaviour |
|---|---|---|
| Form handler — hard delete | `server.py:4725` | `POST /account/delete`; cascades via `cascade_delete_user`; redirects `/` |
| JSON API — soft delete | `server_features.py:531` | `POST /api/account/delete`; sets `deletion_scheduled_for = now + 30d`; clears cookie |
| JSON API — cancel soft delete | `server_features.py:587` | `POST /api/account/delete/cancel`; clears `deletion_scheduled_for` |
| Cascade helper | `queries/auth.py:870` | `cascade_delete_user(user_id)` — schema walk + `DELETE FROM <table>` |
| Session revoke | `queries/auth.py:934` | `revoke_all_user_sessions(user_id)` — UPDATEs `user_sessions.revoked` |
| Legacy session wipe | `queries/auth.py:120` | `delete_sessions_for_user` — DELETE FROM legacy `sessions` |
| Hard-delete cron | `jobs/pipeline_jobs.py:38` | Anonymises user row, cascades personal data, **retains** subs/analytics/bets |
| Migration / schema | `migrations/005_account_deletion.py` | adds `deletion_requested_at`, `deletion_scheduled_for`, `deletion_cancelled_at`, `deleted_at`, `is_deleted` |
| Audit-log constants | `security/audit.py:35–37` | `USER_DELETE_INITIATED`, `USER_DELETE_CANCELLED`, `USER_DELETE_COMPLETED` |

Tests at `tests/test_account_deletion.py` exercise the soft-path SQL and the cron job, but **never invoke either route through the FastAPI app**, so divergence between the two surfaces is not test-covered.

---

## 2. Findings

### CRIT-1 — Form handler `POST /account/delete` performs immediate hard-delete with no grace period

**Where.** `gateway/server.py:4725–4823`.

```python
@app.post("/account/delete")
async def account_self_delete(
    request: Request,
    confirm_email: str = Form(""),
    confirm_password: str = Form(""),
):
    ...
    deleted = db.cascade_delete_user(user_id)   # <-- HARD DELETE
    ...
    response = RedirectResponse("/", status_code=302)
```

**Why it matters.** Migration 005 and the soft-delete API (`server_features.py:531`) were explicitly designed to give users a 30-day reversal window:

- `deletion_scheduled_for = now + 30 * 86400` (`server_features.py:551`)
- `POST /api/account/delete/cancel` un-schedules (`server_features.py:587`)
- Cron `process_scheduled_deletions` only acts when `deletion_scheduled_for <= now AND deletion_cancelled_at IS NULL` (`pipeline_jobs.py:51–57`)

The form-handler bypasses all three. It calls `cascade_delete_user` synchronously, which wipes the user row **and every row in every table with a `user_id` column**. There is no `deletion_requested_at`, no recovery, no email confirmation step. A misclick (or a successful CSRF attack — see HIGH-3) is permanent.

The privacy page (`static/privacy.html:411`) documents this route specifically:

> use the self-service controls in `/settings/privacy` (data export) and `/settings → Delete account` (which calls `POST /account/delete`).

So the *documented, user-facing* GDPR-erasure path is the **only** one without the safety net. The JSON API the safety net exists for is undocumented and apparently called by nothing the user can see — `grep -rn "/api/account/delete" gateway/static/ gateway/templates/ gateway/server.py gateway/server_features.py` returns only the route definitions, no callers.

**Severity rationale.** Permanent destructive operation, no recovery, documented endpoint, no test that exercises the actual route handler. Misclick risk is high (form takes a single password re-entry; see MED-3); CSRF protection mitigates one class of attacker but not session-fixation or stolen-cookie attacks (HIGH-3).

**Fix sketch.** Either delete the form handler and route the settings UI at `/api/account/delete`, or rewrite the form handler to call the soft-delete code path (set `deletion_scheduled_for`, revoke sessions, send confirmation email, redirect to a "Your account will be deleted on …" notice).

---

### HIGH-1 — Stripe customer / subscription is never cancelled or deleted on user delete

**Where.**

- `gateway/server_features.py:559–562` (soft-delete sub-cancel):
  ```python
  c.execute(
      "UPDATE subscriptions SET status = 'cancelled' WHERE user_id = ? AND status = 'active'",
      (user["user_id"],),
  )
  ```
- `gateway/queries/auth.py:870` (`cascade_delete_user`) — issues `DELETE FROM subscriptions WHERE user_id = ?`, never touches Stripe.
- `gateway/jobs/pipeline_jobs.py:38–104` — anonymises user row, retains `subscriptions`, never touches Stripe.

Stripe is reachable elsewhere in the repo (`stripe.Subscription.retrieve` at `billing_routes.py:1322`, `subproduct_access.py:181`, `jobs/reconcile_subscriptions.py:51`, `stripe_webhook_hardening.py:437`) so the SDK is wired up — the deletion flow simply does not call it.

**Why it matters.**

1. **Continued billing of deleted accounts.** Stripe's subscription is the source of truth for `next_invoice_at`; flipping the local row to `'cancelled'` does not stop the next charge. The user has no UI to log in and notice (sessions are revoked, hard path destroys the row).
2. **Orphan webhooks land on anonymised rows.** When Stripe eventually fires `customer.subscription.updated` / `.deleted`, `stripe_webhook_hardening.py:255–264` looks up by `stripe_customer_id`; the soft-deleted user is still there with anonymised email but the customer id retained — the webhook silently mutates a `deleted_<id>@deleted.narve.ai` row. After the hard-delete path the row is gone and the webhook is a no-op, masking the financial discrepancy.
3. **GDPR.** Stripe-side customer record (name, last4, billing address) survives the "right to erasure" deletion. The privacy page promises Art.17 erasure but the Stripe-side personal data is not requested for deletion.

**Severity rationale.** Live financial impact (user is charged after closing the account), GDPR Art.17 violation, and a silent reconciliation black hole.

**Fix sketch.** Both deletion paths should:

```python
import stripe
if user_row["stripe_customer_id"]:
    # 1. Cancel any active subs at_period_end=False (immediate)
    for sub in stripe.Subscription.list(customer=user_row["stripe_customer_id"], status="active").auto_paging_iter():
        stripe.Subscription.delete(sub.id)
    # 2. Delete the customer object (GDPR)
    stripe.Customer.delete(user_row["stripe_customer_id"])
```

Wrap in try/except and log; never block the deletion on Stripe being down.

---

### HIGH-2 — `cascade_delete_user` overrides `ON DELETE SET NULL` and destroys retained-by-design data

**Where.** `gateway/queries/auth.py:870–902`.

```python
def cascade_delete_user(user_id: int) -> dict:
    deleted: dict = {}
    with db.conn() as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for r in rows:
            table = r["name"]
            if table == "users":
                continue
            cols = [c2["name"] for c2 in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if "user_id" in cols:
                cur = c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                ...
        cur = c.execute("DELETE FROM users WHERE id = ?", (user_id,))
```

**Why it matters.** SQLite has FK enforcement enabled (`db.py:261`), so a plain `DELETE FROM users WHERE id = ?` would respect each table's declared `ON DELETE` action. By manually iterating and forcing `DELETE FROM <table>` first, `cascade_delete_user` flattens every policy to "always delete", erasing rows the migration author tagged for retention:

| Table | FK action | What's lost |
|---|---|---|
| `market_takes` (mig 122 line 23) | `ON DELETE SET NULL` | Research record — community predictions/reasonings the platform retains for credibility tracking |
| `discord_guild_links` (mig 64 line 36) | `ON DELETE SET NULL` | `setup_by_user_id` audit trail — orphaning the integration intentionally |
| `feedback_submissions` (mig 130 line 62) | `ON DELETE SET NULL` | Product-feedback history (intended to outlive the submitter for triage) |
| `share_metrics` (mig 114 line 46) | `ON DELETE SET NULL` | Public sharing analytics |
| `security_events` (mig 72 line 26) | `ON DELETE SET NULL` | **Security audit trail** — destroying the very evidence a forensic investigation would need after a takeover-then-self-delete |
| `system_secrets` (mig 174 line 43) | `ON DELETE SET NULL` | `updated_by` admin trail |
| `subproduct_feature_flags` (mig 186 line 66/128) | `ON DELETE SET NULL` | Admin change history on feature flags |
| `take_reports` (mig 123) | partially `SET NULL` | Moderation trail |
| (further) | several `ON DELETE SET NULL` declarations | see `grep -n "ON DELETE SET NULL" migrations/*.py` for the full list |

The pipeline-jobs hard-delete explicitly **does not** do this (`pipeline_jobs.py:88` retains `subscriptions, analytics_events, user_bet_history`), so the two flows diverge on what survives.

**Severity rationale.** Silent loss of audit/forensic data; behaviour contradicts schema declarations; affects 10+ tables.

**Fix sketch.** Replace `cascade_delete_user` with a single `DELETE FROM users WHERE id = ?` and rely on SQLite's FK engine (which is already enabled). Tables that need pre-deletion (sessions, password_resets, etc.) already have `ON DELETE CASCADE`; tables that need retention have `ON DELETE SET NULL`. The schema is right; the helper is wrong.

---

### HIGH-3 — `/account/delete` accepts only password re-entry; no 2FA / re-auth challenge on a destructive action

**Where.** `gateway/server.py:4760–4767`.

```python
typed_email = (confirm_email or "").strip().lower()
stored_email = (db_user["email"] or "").strip().lower()
if not typed_email or typed_email != stored_email:
    raise HTTPException(status_code=400, detail="Email confirmation does not match")
if not confirm_password or not db.verify_password(
    confirm_password, db_user["password_hash"], db_user["password_salt"]
):
    raise HTTPException(status_code=401, detail="Password is incorrect")
```

**Why it matters.** This is the *only* gate between an attacker holding a live session cookie and **permanent** account destruction. Compare with `/profile/password` (`server.py:4823`), which also requires current password but is *reversible* — the user can request a reset. Account deletion is one-way, but it is gated by less.

Specifically:

- **No 2FA challenge.** A user who set up TOTP/email-OTP at signup is not asked for it again on delete. A stolen session that has never seen the 2FA factor can still delete the account.
- **No fresh-auth requirement.** A 30-day-old session cookie + a guessed/phished password gets you to the destructive endpoint.
- **No email confirmation link.** The pattern used by every major SaaS — send a confirmation email with a single-use token — is absent on the form path. (The API path does send `account_deletion_confirmation`, but the form path does not.)

CSRF is enforced (POST handler, not in `_CSRF_EXEMPT_POSTS` at `server.py:1124`) so cross-site forgery is blocked, but session-hijack / stolen-cookie / shoulder-surfed-password attacks land.

**Severity rationale.** A reversible destructive action would be MED; a permanent one with no fallback is HIGH.

**Fix sketch.** Require 2FA challenge for users with 2FA enabled. Send a confirmation email and require the user to click a tokenised link to actually trigger the soft-delete (the API endpoint can become the email-link handler).

---

### HIGH-4 — Soft-delete API does not block deletion while impersonating

**Where.** `gateway/server_features.py:531`. Compare with the form handler at `server.py:4778–4783`:

```python
# server.py form handler — checks impersonation
try:
    from impersonation import is_impersonating
    if is_impersonating(request):
        raise HTTPException(status_code=403, detail="Cannot delete an account while impersonating")
except ImportError:
    pass
```

The `api_account_delete` function (`server_features.py:532`) has **no equivalent check**. An admin who is currently impersonating user X can hit `/api/account/delete` (or its cancel sibling) and trigger / revoke X's soft-delete without X's knowledge.

`current_user(request)` at `server.py:2068–2084` returns the target's identity during impersonation, so the API silently treats the admin's action as X's own — including the audit row (which has no impersonation marker on this endpoint, since the endpoint doesn't log to the audit table at all).

**Severity rationale.** Insider-attack vector; no audit trail of who actually triggered it.

**Fix sketch.** Add the same `is_impersonating(request)` guard at the top of both `api_account_delete` and `api_account_delete_cancel`. Also call `security.audit.log_action(..., USER_DELETE_INITIATED, ...)` from the soft-path so the audit log captures who.

---

### MED-1 — `jwt_invalidated_before` is written but never read

**Where.** Set on soft-delete at `server_features.py:556`:

```python
"UPDATE users SET deletion_requested_at = ?, deletion_scheduled_for = ?, "
"deletion_cancelled_at = NULL, jwt_invalidated_before = ? WHERE id = ? ",
(now, deletion_scheduled_for, now, user["user_id"]),
```

Also set on password reset (`server.py:5189`, `server_features.py:335`).

`grep -rn "jwt_invalidated_before" gateway/` confirms the column is **written in three places and read in zero**. Session validation in `current_user`/`get_session` does not compare session creation time against `jwt_invalidated_before`.

**Why it matters.** The audit comment at `server_features.py:556` implies the bump is meant to invalidate already-issued session cookies that might survive the `DELETE FROM sessions` (defence-in-depth against the session being re-issued mid-transaction by another worker). It does nothing of the sort — any session left intact by the delete would still validate.

In practice the soft-delete also runs `DELETE FROM sessions WHERE user_id = ?` (`server_features.py:565`) so live sessions are killed by the DELETE, not by the JWT bump. Still, the column gives a false sense of defence-in-depth.

**Severity rationale.** Latent bug, not actively exploitable today because session DELETE covers it. Becomes a problem if anyone reorders the statements or migrates to JWT-only auth.

**Fix sketch.** Either wire `jwt_invalidated_before` into `validate_user_session` / `get_session` so any session created before the timestamp is rejected, or drop the column writes.

---

### MED-2 — `sessions` (legacy) table deletion bypasses the hardened `user_sessions` UPDATE flag

**Where.** `gateway/server_features.py:565`:

```python
c.execute("DELETE FROM sessions WHERE user_id = ?", (user["user_id"],))
```

vs the form handler `server.py:4794–4797` which calls `db.revoke_all_user_sessions(user_id)` first — that UPDATEs `user_sessions.revoked = 1` (queries/auth.py:934–943).

**Why it matters.** The codebase keeps two parallel session stores:

- legacy `sessions` (`db.py:36`) — token in plaintext, used by `current_user` fallback (`server.py:2099–2111`)
- hardened `user_sessions` (`migrations/007_user_sessions_hardening.py:20`) — token hash, used by `SessionMiddleware`

The soft-delete API only deletes from legacy `sessions`. Hardened `user_sessions` rows survive the UPDATE-not-DELETE path until `cascade_delete_user` runs at hard-delete time. During the 30-day soft window, a hardened session cookie issued before the soft-delete continues to validate (it has `user_id` and the user row still exists and is not yet `is_deleted = 1`).

**Severity rationale.** Inconsistent session-invalidation surface; in practice mitigated because the hardened middleware also reads the user row and could be wired to refuse `deletion_scheduled_for IS NOT NULL` users — but it isn't (no `is_deleted` / `deletion_scheduled_for` check in `current_user`).

**Fix sketch.** In `api_account_delete`, after deleting from legacy `sessions`, also call `db.revoke_all_user_sessions(user["user_id"])` to UPDATE the hardened table. Optionally also block login for `is_deleted = 1 OR deletion_scheduled_for IS NOT NULL` users.

---

### MED-3 — Confirmation token mismatch between flows weakens UX consistency

**Where.**

- Form handler asks for `confirm_email` (must equal stored email) + `confirm_password` (must match hash).
- API handler asks only for `{"confirm": "DELETE"}` literal string (`server_features.py:547`).

```python
# API:
if (body.get("confirm") or "").strip().upper() != "DELETE":
    return JSONResponse({"error": "Type DELETE to confirm"}, status_code=400)
```

The API path does NOT verify the password. It relies on session-cookie auth alone.

**Why it matters.** A stolen session cookie + a single JSON POST is enough to soft-delete via the API. The 30-day recovery window mitigates damage (the user can sign back in and cancel during the grace period — IF they notice — IF the attacker hasn't also changed the email, which they can via `/profile/email` similarly password-gated only on the form path).

**Severity rationale.** Lower-severity sibling to HIGH-3; same root cause (over-reliance on session cookie for a destructive op).

**Fix sketch.** Either drop the form path (CRIT-1 fix) or harmonise — both paths should require a fresh password+2FA challenge.

---

### MED-4 — Privacy page documents only the form route; soft-delete is invisible to users

**Where.** `static/privacy.html:411`:

> use the self-service controls in `/settings/privacy` (data export) and `/settings → Delete account` (which calls `POST /account/delete`).

**Why it matters.** The privacy commitment uses the *immediate* hard-delete route as the GDPR Art.17 surface, advertising no recovery window. This is consistent with what `/account/delete` actually does (CRIT-1) — but the soft-delete with grace period that the cron job, schema, and `account_deletion_confirmation` email template are all built for is **not reachable from any documented UI**.

This is a documentation/feature gap, not an attack: if the user expects "you have 30 days to change your mind" (an industry-standard GDPR friendliness), they will be disappointed.

**Severity rationale.** UX / privacy-policy compliance gap.

**Fix sketch.** Pick one flow. Most modern SaaS uses soft-delete-with-confirmation-email. If the choice is to ship that, update privacy.html and route `/settings` UI to `/api/account/delete`.

---

### LOW-1 — `current_user` does not block soft-deleted users from logging back in or being served

**Where.** `gateway/server.py:2057–2130`. No check on `is_deleted` or `deletion_scheduled_for`.

**Why it matters.** During the 30-day soft-delete window, the user row still has `is_deleted = 0` (set to 1 only at hard-delete by `pipeline_jobs.py:70`) and `password_hash` is intact. A user could:

- log back in (intentional — recovery flow)
- continue to be returned by analytics queries (the existing queries already filter `is_deleted = 0`, which is fine, but the deletion_scheduled_for users are not yet flagged)

This may be intentional (it's how cancellation works), but **a user who has soft-deleted is not informed at login that the account is scheduled for deletion**. There is no `/api/account/delete/status` or similar surface.

**Severity rationale.** UX papercut; recoverable behaviour, but the user can be surprised when the hard-delete cron runs.

**Fix sketch.** On every authenticated GET in the 30-day window, show a banner: "Your account is scheduled for deletion on <date>. [Cancel deletion]". The cancel button already exists at `/api/account/delete/cancel`.

---

### LOW-2 — Audit log entry skipped in two of the three flows

**Where.**

- `server.py:4824–4833` — form path logs `USER_DELETE_COMPLETED`.
- `server_features.py:531` — API soft-path does **not** log to `security.audit`.
- `server_features.py:587` — cancel does **not** log.
- `jobs/pipeline_jobs.py:38` — cron hard-delete does **not** log.

The constants `USER_DELETE_INITIATED` and `USER_DELETE_CANCELLED` are defined (`security/audit.py:35–36`) but never written.

**Why it matters.** Post-incident review (was this deletion the user's intent, or an admin/attacker?) has no canonical record except an `log.info` line in the FastAPI process logs, which rotate.

**Severity rationale.** Forensics gap, not directly exploitable.

**Fix sketch.** Add `audit.log_action(...)` calls at each transition (initiate, cancel, complete-by-cron).

---

### LOW-3 — Anonymisation uses predictable replacement values

**Where.** `gateway/jobs/pipeline_jobs.py:64`:

```python
anon_email = f"deleted_{user_id}@deleted.narve.ai"
...
c.execute(
    "UPDATE users SET email = ?, username = ?, "
    "is_deleted = 1, deleted_at = ?, "
    "password_hash = '', password_salt = '', "
    ...
    (anon_email, f"[deleted_{user_id}]", now, user_id),
)
```

The replacement email/username embed `user_id`. If anyone outside the operator knows a victim's user_id (e.g. it appeared in a public source profile URL, share link, or via referrals leaderboard), they can reverse-lookup which row corresponded to which user post-deletion.

Per GDPR Art.17 strictness, pseudonymous data that is straightforwardly reversible to a real person remains "personal data". For most threat models, an integer that an attacker would need to have separately recorded is fine; for a strict reading, hash-and-salt the user_id.

**Severity rationale.** Pedantic GDPR; defensible under most pragmatic readings.

**Fix sketch.** `anon_email = f"deleted_{secrets.token_hex(8)}@deleted.narve.ai"` and drop the user_id-in-username pattern, or hash it.

---

### INFO-1 — `process_scheduled_deletions` is single-pass and silent on failures

**Where.** `gateway/jobs/pipeline_jobs.py:38–104`.

The loop catches `Exception` per-user and continues, which is right. But the cron runs daily at 02:00 UTC (line 215) and there is no alert / metric / retry on failure. A user whose hard-delete fails (FK violation, disk-full, etc.) will be retried only on the next daily tick. There is no upper bound on how many days a "scheduled" deletion can sit unprocessed.

**Severity rationale.** Operational, not security.

**Fix sketch.** Emit a Prometheus counter on each failure; alert if `deletion_scheduled_for < now() - 7d AND is_deleted = 0`.

---

### INFO-2 — The two flows fan out different emails

- Form path: **no email sent** (immediate hard-delete, redirect to `/`).
- API path: `account_deletion_confirmation` email sent on soft-delete (`server_features.py:570–578`).
- Cron path: `account_deleted` final email at hard-delete (`pipeline_jobs.py:92–97`, intentional bounce).

If a user uses the documented `/account/delete`, they receive **no confirmation email at all** — they may not even realise the delete went through unless they notice they are logged out. The transactional email templates for the soft path exist (`email_system/__init__.py:24`, `email_system/service.py:198`) but the form path doesn't fire them.

**Severity rationale.** UX gap; ties into MED-4.

---

## 3. Scope checklist (requested by audit brief)

| Item | Status | Detail |
|---|---|---|
| CSRF | **OK** | `/account/delete` and `/api/account/delete` are POST handlers, not in `_CSRF_EXEMPT_POSTS` (`server.py:1124`). CSRF middleware (`server.py:1267`) enforces token. Origin check on every POST. |
| Password re-entry | **PARTIAL** | Form path requires email + password. API path requires only `confirm: "DELETE"` literal — **no password verify** (MED-3). No 2FA challenge on either (HIGH-3). |
| Soft-delete grace period | **PARTIAL** | Schema (`migrations/005`), cron (`pipeline_jobs.py:38`), and API endpoint all built around 30-day window. But the documented form route bypasses it entirely (CRIT-1). |
| Hard-delete cascade across all FKs | **WRONG** | `cascade_delete_user` overrides `ON DELETE SET NULL` declarations and destroys data the schema intended to retain (HIGH-2). Affects market_takes, security_events, feedback_submissions, share_metrics, and 6+ more tables. |
| Stripe customer deletion | **MISSING** | Neither path calls Stripe. Customers continue to be billed for closed accounts (HIGH-1). |
| Session revocation on delete | **PARTIAL** | Form path: revokes `user_sessions` via UPDATE + deletes `sessions` via the cascade. API path: deletes `sessions` only, leaves `user_sessions.revoked = 0` until cron runs 30 days later (MED-2). `jwt_invalidated_before` is bumped but never read (MED-1). |

---

## 4. Recommendation

1. **Pick one deletion flow.** Either ship the soft-delete (with email-link confirm + 30-day recovery) as *the* deletion path and remove the immediate-hard-delete form handler, or ship hard-delete-with-2FA-challenge and remove the soft-delete machinery. Living with both is the source of CRIT-1, HIGH-4, MED-2, MED-3, and the divergent email behaviour (INFO-2).

2. **Talk to Stripe before deleting.** Both paths must cancel Stripe subscriptions and delete the Stripe customer object (HIGH-1).

3. **Trust the schema, not the helper.** Replace `cascade_delete_user`'s schema-walk with a single `DELETE FROM users WHERE id = ?` and let `ON DELETE CASCADE` / `SET NULL` do their declared job (HIGH-2).

4. **Add the missing audit-log entries and the missing 2FA challenge** to harden against insider/stolen-session abuse (HIGH-3, HIGH-4, LOW-2).

No code changes performed in this audit (per task hard rule).
