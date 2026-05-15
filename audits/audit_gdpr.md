# Adversarial Audit — GDPR Compliance (Data Export + Account Deletion)

Date: 2026-05-15
Auditor: Claude (Opus 4.7, 1M context)
Scope: every route/function in `gateway/` that performs data export or account
deletion under GDPR Art. 15 (right of access / portability) and Art. 17 (right
to erasure).

Files reviewed in depth:

- `/Users/shocakarel/Habbig/gateway/export_routes.py` (route handlers, signed
  download URLs, the *inline* ZIP builder)
- `/Users/shocakarel/Habbig/gateway/exports/generator.py` (canonical ZIP builder,
  redaction helpers, signed-URL helpers, ARQ entry point)
- `/Users/shocakarel/Habbig/gateway/jobs/export_jobs.py` (ARQ jobs:
  `generate_data_export`, `cleanup_expired_data_exports`)
- `/Users/shocakarel/Habbig/gateway/jobs/pipeline_jobs.py`
  (`process_scheduled_deletions` — the hard-delete job)
- `/Users/shocakarel/Habbig/gateway/server.py` (`POST /account/delete`,
  `POST /admin/users/{id}/delete`, `POST /admin/users/bulk` delete branch)
- `/Users/shocakarel/Habbig/gateway/server_features.py` (`POST /api/account/delete`,
  `POST /api/account/delete/cancel`)
- `/Users/shocakarel/Habbig/gateway/admin_routes.py` (`/admin/users/{id}/export`
  shortcut)
- `/Users/shocakarel/Habbig/gateway/queries/auth.py` (`cascade_delete_user`)
- `/Users/shocakarel/Habbig/gateway/queries/data_exports.py`
- `/Users/shocakarel/Habbig/gateway/migrations/005_account_deletion.py`,
  `030_data_exports.py`, plus the full set of 108 CREATE TABLE statements
  across `migrations/` and `db.py::SCHEMA` for coverage analysis

Routes/functions catalogued:

| # | Path / function | File:line | Type |
|---|-----------------|-----------|------|
| 1 | `POST /api/account/export` (`api_request_export`) | `export_routes.py:277` | export enqueue |
| 2 | `GET  /api/account/exports` (`api_list_exports`) | `export_routes.py:298` | export listing |
| 3 | `GET  /api/account/export/{id}/download` (`api_download_export`) | `export_routes.py:322` | export download |
| 4 | `GET  /settings/privacy` (`privacy_settings_page`) | `export_routes.py:355` | export UI |
| 5 | `_build_zip(user_id, zip_path)` | `export_routes.py:129` | export builder (inline / ACTUAL) |
| 6 | `_run_export(request_id)` | `export_routes.py:234` | thread-pool worker |
| 7 | `exports.generator.build_zip(user_id, target_path)` | `exports/generator.py:793` | export builder (canonical / UNUSED at runtime) |
| 8 | `exports.generator._collect(user_id)` | `exports/generator.py:207` | per-table fetch (50+ tables) |
| 9 | `exports.generator.generate(export_id)` | `exports/generator.py:906` | ARQ-driver |
|10 | `jobs.export_jobs.generate_data_export(export_id)` | `jobs/export_jobs.py:20` | ARQ entry (never enqueued from the route) |
|11 | `jobs.export_jobs.cleanup_expired_data_exports()` | `jobs/export_jobs.py:30` | TTL sweep (cron 03:30 UTC) |
|12 | `GET  /admin/users/{id}/export` (`users_export_data`) | `admin_routes.py:2350` | admin GDPR shortcut |
|13 | `POST /account/delete` (`account_self_delete`) — form | `server.py:4725` | self-delete (immediate hard delete via `cascade_delete_user`) |
|14 | `POST /api/account/delete` (`api_account_delete`) — JSON | `server_features.py:531` | self-delete (soft, 30-day window) |
|15 | `POST /api/account/delete/cancel` | `server_features.py:587` | cancel pending deletion |
|16 | `POST /admin/users/{id}/delete` (`admin_delete_user`) | `server.py:6187` | admin hard-delete (3-table) |
|17 | `POST /admin/users/bulk` delete branch | `server.py:6219`+ | admin bulk hard-delete (3-table) |
|18 | `queries.auth.cascade_delete_user(user_id)` | `queries/auth.py:870` | schema-introspecting cascade |
|19 | `jobs.pipeline_jobs.process_scheduled_deletions()` | `jobs/pipeline_jobs.py:38` | soft → hard delete sweep |

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 2 |
| High     | 5 |
| Medium   | 7 |
| Low      | 4 |
| Info     | 3 |
| **Total**| **21** |

---

## Top 3 findings

1. **CRIT-1** — Two parallel deletion code paths produce drastically different
   results for the same user action. `POST /api/account/delete`
   (`server_features.py:531`) sets a 30-day soft-delete flag and lets
   `process_scheduled_deletions` (`jobs/pipeline_jobs.py:38`) hard-delete from
   only 7 explicit tables, then merely anonymises the `users` row. Meanwhile
   `POST /account/delete` (`server.py:4725`) calls `cascade_delete_user`
   (`queries/auth.py:870`) which introspects `sqlite_master` and immediately
   purges every table whose row has a `user_id` column, **deletes the `users`
   row entirely** (no 30-day window, no anonymisation, no recovery). Both
   routes are mounted, both authenticate the same user, and the UI may invoke
   either depending on the surface. Users get a different GDPR outcome based
   on which button their browser submits to — and the soft path silently
   leaves dozens of personal tables intact for 30 days (and many tables intact
   indefinitely, see HIGH-1).

2. **CRIT-2** — Download-URL HMAC secret has a non-fatal, **guessable
   fallback** in `export_routes.py:_export_secret`
   (`export_routes.py:71-80`): if neither `GATEWAY_COOKIE_SECRET` nor
   `DATA_EXPORT_SIGNING_KEY` is set, the secret derives from the literal
   string `f"dataexport:{EXPORT_DIR}"` — a directory path that defaults to
   `/tmp/narve-exports` and is leaked in startup logs. An attacker who knows
   (or guesses) the export directory can forge a valid `?u=&e=&s=` triple
   for any `(export_id, user_id, expires_at)` and download another user's
   ZIP. The canonical helper in `exports/generator.py:_signing_secret`
   correctly **raises `RuntimeError`** if no secret is set
   (`exports/generator.py:93-99`); the two implementations contradict each
   other, and the route uses the unsafe one.

3. **HIGH-1** — Hard-delete sweep (`process_scheduled_deletions`,
   `pipeline_jobs.py:60-89`) only deletes from 7 explicit tables
   (`sessions`, `password_resets`, `email_unsubscribes`, `user_topics`,
   `intelligence_conversations`, `gifted_subscriptions`,
   `user_market_credentials`, `user_market_views`, `feedback_submissions`).
   The schema has **at least 60+ user-keyed tables** (see Tables-not-covered
   list below). Predictions, conversations messages, bet history, watchlist,
   referrals, takes, votes, follows, collections, notifications,
   engagement_events, audit_log targets, push_subscriptions, webhooks,
   embeds, telegram/discord connections, polymarket/kalshi connections,
   API keys, backtests, etc. are **all retained for the soft-delete user**
   despite their stated GDPR purpose. The hardened cascade exists
   (`cascade_delete_user`) but the scheduled path doesn't call it.

---

## Findings (full)

### CRIT-1 — Two deletion code paths with divergent semantics

`gateway/server.py:4725-4800` (form `POST /account/delete`) immediately hard-
deletes via `db.cascade_delete_user(user_id)` which DELETEs the `users` row
and every row in every `user_id`-keyed table.

`gateway/server_features.py:531-584` (JSON `POST /api/account/delete`) flips
`deletion_scheduled_for = now + 30d` and lets a nightly cron remove the user
30 days later (and even then only from 7 tables, see HIGH-1).

Both routes accept different request types but resolve to the same user.
The 30-day "recovery window" the JSON path documents is **bypassed entirely**
by the form path. Users who hit the form endpoint get no chance to cancel.

Fix: pick one. If the soft-delete window is required (e.g. by the
`account_deletion_confirmation` email template and GDPR-compliant grace
period), retire `account_self_delete` or have it set the deletion flag rather
than invoke `cascade_delete_user`. If immediate erasure is required, retire
the soft path and call the hardened cascade synchronously.

### CRIT-2 — Forgeable signed download URLs (route layer)

`export_routes.py:71-80`:

```python
def _export_secret() -> bytes:
    raw = (
        os.environ.get("GATEWAY_COOKIE_SECRET")
        or os.environ.get("DATA_EXPORT_SIGNING_KEY")
        or f"dataexport:{EXPORT_DIR}"
    )
    return hashlib.sha256(raw.encode()).digest()
```

The fallback string is deterministic, low-entropy, and `EXPORT_DIR` (env
`DATA_EXPORT_DIR`) is leaked in startup logs/RUNBOOK.md. A blank-deployed
instance signs every download URL with `sha256(b"dataexport:/tmp/narve-exports")`.

`api_download_export` does NOT require a session cookie
(`export_routes.py:323-325`), so an attacker armed with the forged signature
can hit `/api/account/export/{any_id}/download?u={any_user}&e={any_future}&s={sig}`
and exfiltrate the ZIP from a path read off `data_export_requests.file_path`.
That ZIP contains password-redacted-but-otherwise-complete PII for the target
user.

Mitigation already exists in `exports/generator.py:73-99` (the canonical
helper raises `RuntimeError` instead of falling back). The route layer never
calls that helper — see HIGH-2.

Fix: delete the fallback branch in `_export_secret` and raise `RuntimeError`
as in `exports/generator.py`. Add a startup assertion that
`DATA_EXPORT_SIGNING_SECRET` is set when `_export_routes.register()` runs.

### HIGH-1 — Hard-delete misses ~60 user-keyed tables (see *Tables not covered* below)

`jobs/pipeline_jobs.py:60-89` enumerates 7 hard-coded `DELETE FROM` statements.
The comment "NOTE: subscriptions, analytics_events, user_bet_history retained"
is true but **massively understates the scope** — the actual list of tables
retained beyond the soft delete includes everything from `user_predictions` to
`market_takes` to `embed_widgets` to `api_keys` to `webhook_subscriptions`,
i.e. essentially the entire bundle the export ZIP is supposed to surface.

Net effect: a user who soft-deletes their account stays present in the
database under everything except a handful of curated tables. The
`is_deleted = 1` flag is supposed to make queries skip them, but only the
queries that bother to add `COALESCE(is_deleted, 0) = 0` honour it. Greedy
SELECT-everything queries (the export bundle itself, admin tooling, the
audit log) still see their data.

Fix: invoke `cascade_delete_user(user_id)` from `process_scheduled_deletions`
instead of the hand-rolled DELETE list, and only then anonymise the residual
`users` row.

### HIGH-2 — Two ZIP builders, the wrong one is wired up

`export_routes.py:129-231` implements `_build_zip` *inline* and only covers:

- `users`
- `subscriptions`
- `user_predictions`
- `saved_predictions` (defensive)
- `notifications` (defensive)
- metadata + README

`exports/generator.py:207-656` implements `_collect()` covering ~50 tables
correctly with redaction helpers. The ARQ job at
`jobs/export_jobs.py:20-27` calls into it.

But `_enqueue_export` in `export_routes.py:269-271`:

```python
def _enqueue_export(request_id: int) -> None:
    _executor.submit(_run_export, request_id)   # local thread-pool
```

The thread-pool runs `_run_export → _build_zip` (the **inline minimal one**).
The ARQ-registered `generate_data_export` is never called by the route.
GDPR users get the inline-minimal ZIP at runtime; the comprehensive ZIP
ships only via tests (`tests/test_data_export.py` imports `exports.generator`
directly).

Fix: replace `_enqueue_export` with an ARQ enqueue (or have `_run_export` call
`exports.generator.generate(request_id)`).

### HIGH-3 — Download endpoint omits session auth → relies entirely on HMAC

`export_routes.py:322-352` (`api_download_export`) is intentionally
session-less. Combined with CRIT-2 (forgeable signature) or the lifetime of
any unleaked link (`EXPORT_TTL_DAYS = 7`), this gives **full PII exfil** to
anyone with the URL fragment. Real-world risk: URL pasted in chat, in a
support ticket, in a third-party password manager auto-fill log, in an HTTP
proxy capture, in an analytics scrubber.

Even with CRIT-2 fixed, the URL still carries the entire authentication
token for 7 days. There's no audit log of downloads, no IP binding, no
re-auth challenge, no `Strict-Transport-Security` enforcement at the route
level.

Fix: require a session cookie OR a short-lived one-time bearer; log the
download; bind to the requesting user's session if possible.

### HIGH-4 — Rate-limit treats failed exports the same as successful ones

`export_routes.py:284-290`:

```python
last_ts = db.last_user_data_export_ts(user_id)
if last_ts and int(time.time()) - int(last_ts) < RATE_LIMIT_SECONDS:
    raise HTTPException(status_code=429, ...)
```

`last_user_data_export_ts` reads the most recent `requested_at` regardless of
status (`queries/data_exports.py:56-64`). A failed export (status `failed`)
counts toward the 24h rate limit. Per Art. 12 § 5 GDPR a controller must
respond to a subject-access request without "undue delay". Blocking the user
for 24h because of an internal error is the controller's fault, not the
user's, and a court/regulator would treat that as a refusal.

Fix: filter the rate-limit query to `status IN ('ready', 'processing',
'pending')` or simply ignore `failed` entries.

### HIGH-5 — Admin delete routes bypass the cascade

`server.py:6201-6204` (`admin_delete_user`):

```python
c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
c.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
c.execute("DELETE FROM users WHERE id = ?", (user_id,))
```

`server.py:6248-6254` (bulk delete branch) does the same three statements.

With `PRAGMA foreign_keys = ON` and most user-id-keyed tables declared
`REFERENCES users(id) ON DELETE CASCADE`, the FK cascade does fire. **But:**

- Tables WITHOUT a FK constraint (those declaring `user_id INTEGER NOT NULL`
  without `REFERENCES users(id)` — at least 30 of them, see the
  `audit_fk_integrity.md` audit for the full list, or grep `migrations/`)
  are silently left orphaned. Their rows survive after the user row vanishes,
  contradicting the GDPR Art. 17 commitment.
- Tables with `ON DELETE SET NULL` (e.g. `market_takes`, `feedback_items`,
  `discord_servers.setup_by_user_id`) are intentional anonymisation, but the
  admin path doesn't audit-log what was nulled out — making a regulator audit
  trail incomplete.
- `audit_log.target_id` is a TEXT column with no FK; admin actions against
  the user remain forever with `target_description` containing the user's
  email. Not erased.

Fix: `admin_delete_user` and the bulk delete branch should call
`cascade_delete_user(user_id)` after revoking sessions, same as the form
route. Audit-log should record the count of cascaded rows per table.

### MED-1 — Export thread pool runs without session re-validation

`_run_export` (`export_routes.py:234-266`) loads the export request from
`data_export_requests`, reads `user_id`, then calls `_build_zip(user_id, …)`.
By the time the thread pool worker runs (seconds-to-minutes later), the
session that triggered the export may have been:

- logged out (cookie invalidated)
- revoked by an admin
- the user's password may have been changed (bumping
  `jwt_invalidated_before`)
- the account may have been suspended

The worker has no awareness of any of that — it builds the ZIP with the
*current* user data regardless. A user who clicks Export, then immediately
changes their password (a common security reflex), still receives a ZIP
that reflects the *post-rotation* DB state, which may include the new
password hash format and any newer entries.

More importantly: an attacker who hijacks a session, enqueues an export,
then has their session revoked by the user still gets the ZIP delivered to
the email on file — but the signed URL works regardless of session state
because of HIGH-3.

Fix: snapshot `jwt_invalidated_before` at enqueue time; on worker start,
re-validate that the user's account is still in a state where export is
permitted, and that no `account-delete` event invalidated the request.

### MED-2 — `cascade_delete_user` uses Python-level f-string for table names

`queries/auth.py:889-898`:

```python
cols = [c2["name"] for c2 in c.execute(f"PRAGMA table_info({table})").fetchall()]
...
cur = c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
```

`table` comes from `sqlite_master.name`, which in normal operation is
controlled by migration code — safe. BUT a writable-DB attacker who can
inject a malicious table (e.g. via a misconfigured `c.executescript` path,
a SQL-injection foothold elsewhere, or a future migration with user-supplied
content) gains arbitrary SQL execution at delete time.

Fix: enumerate against a whitelist of expected user-keyed tables; raise on
unknown. Defense-in-depth — the introspection-driven approach is convenient
but trusts `sqlite_master` completely.

### MED-3 — Newsletter/enquiries PII is never deleted or anonymised

`db.py:71-78` (`enquiries`) and `db.py:96-101` (`newsletter_subscribers`)
both store an `email` column without any FK to `users`. When a user with
matching email deletes their account:

- their enquiry message + email remains in the table forever
- their newsletter subscription remains and continues to receive sends
- the `referred_by` linkage is by `referral_code`, not by user id, so the
  subscriber row survives but the referral chain becomes unattributable
- the user's `email` is now `deleted_<id>@deleted.narve.ai` (soft delete) or
  gone entirely (hard delete), but `newsletter_subscribers.email` still
  contains the real address

Net effect: a soft- or hard-deleted user keeps receiving newsletter sends
(if `send_newsletter` doesn't join `users.is_deleted`). I did not exhaustively
audit every newsletter send path — but the `gateway/jobs/newsletter_blast_jobs.py`
and `gateway/queries/newsletter.py` modules query `newsletter_subscribers`
directly, not through `users`, so the email persists.

Fix: in `process_scheduled_deletions`, also `DELETE FROM newsletter_subscribers
WHERE email = ?` for the user's pre-anonymisation email, and `DELETE FROM
enquiries WHERE email = ?` (or anonymise to the same `deleted_{id}@...`
placeholder, which would be the safer audit-trail-preserving option).

### MED-4 — Export bundle references a non-existent table

`exports/generator.py:296-301`:

```python
bundle["notifications"] = _safe_query(
    c,
    "SELECT * FROM email_send_log WHERE user_id = ? "
    "ORDER BY sent_at DESC LIMIT 1000",
    (user_id,),
)
```

There is no `email_send_log` table in any migration or in `db.py::SCHEMA`.
`_safe_query` silently returns `[]` (it catches `no such table`). So the
GDPR export bundle ships an empty `notifications/history.{csv,json}` and
the affected user can't see what emails were sent to them — a soft Art. 15
breach (the controller does have the data, it's just stored in a third-party
provider's log, and we don't surface it).

Note: a separate `notifications_feed` field maps to the `notifications`
table from migration 026 — that's the in-app feed. The email send log is
separate. There's no migration creating `email_send_log`; the comment at
`generator.py:294` calls it out as "best-effort".

Fix: either create an `email_send_log` table and have email-send paths
write to it, OR remove the dead `email_send_log` block and document the
gap in the README inside the ZIP.

### MED-5 — Soft-delete state visible to readers who don't filter by `is_deleted`

`users.is_deleted` exists (`migrations/005_account_deletion.py:18`), and
many readers correctly filter it (`db_referrals.py`, `notification_jobs.py`,
`email_jobs.py`, `generate_weekly_reports.py`). But the `_collect`
implementation in `exports/generator.py:218-220` SELECTs `* FROM users WHERE
id = ?` with no filter — meaning even a soft-deleted user's record can be
exported via the same flow (they probably can't authenticate, but `admin_routes.py`'s
admin export shortcut hits the same path). Less of a leak, more of a "stale
data being treated as live".

The audit-log target row also keeps `target_description` containing the
user's email forever — see HIGH-5.

Fix: have `_collect` short-circuit when `users.is_deleted = 1`, surfacing
just a "this account is deleted" stub.

### MED-6 — Race: delete cancel + scheduled deletion fire in same window

`server_features.py:594` (cancel) and `pipeline_jobs.py:38` (cron) are
unsynchronised. If the cron starts iterating *just before* the user clicks
Cancel:

1. Cron fetches rows at line 51 with `deletion_cancelled_at IS NULL`.
2. User clicks Cancel; transaction commits at `server_features.py:599`,
   setting `deletion_cancelled_at = now, deletion_scheduled_for = NULL`.
3. Cron loops to the user's row and executes the anonymisation UPDATE —
   it does NOT re-check the cancel field.

The user thinks they cancelled; the system thinks they didn't; the cron
hard-deletes them. This is hard to trigger but legally catastrophic.

Fix: inside the `for r in rows` loop, re-query the cancel timestamp
under a transaction, OR convert the SELECT+UPDATE pair into a single
`UPDATE … WHERE deletion_cancelled_at IS NULL AND deletion_scheduled_for
<= ?` and skip the row if the rowcount is zero.

### MED-7 — Rate limit bypassed via admin shortcut

`admin_routes.py:2350-2400` (`users_export_data`) lets an admin pull a
CSV of any user's account data with NO rate limit and NO awareness of the
1-export-per-day cap that applies to the user. It's a small CSV (just the
account row), not the full ZIP — so the practical leak is small — but a
compromised admin account can iterate over every user and exfiltrate
emails / created_at / is_admin status / default_dashboard cheaply. The
audit-log call at line 2381 uses `USER_PROMOTE_ADMIN` as a "neutral
admin-read action" — abusing an unrelated audit code defeats the audit
trail.

Fix: introduce a dedicated `USER_GDPR_EXPORT` action; rate-limit
per-admin (10/hour); confirm the export request was made via a real GDPR
ticket, not as a casual click.

### LOW-1 — `_export_secret` SHA256-of-string vs HMAC-of-string

Even with the right env var set, `_export_secret` returns
`hashlib.sha256(raw.encode()).digest()` and feeds it as the HMAC key. That's
correct usage (HMAC absorbs arbitrary-length keys), but it discards
information when `GATEWAY_COOKIE_SECRET` is already a 32-byte cryptographic
value — the SHA256 pre-hash adds no security. Minor; would only show up in
key-extension attacks against the underlying secret, which aren't realistic
for HMAC-SHA256.

### LOW-2 — Download response lacks `Content-Disposition` nosniff

`api_download_export` returns `FileResponse` with `media_type="application/zip"`
and a filename. No `X-Content-Type-Options: nosniff`, no `Cache-Control:
private, no-store`. A proxy or browser extension might cache the response.
Fix: emit `Cache-Control: private, max-age=0, no-store` and
`X-Content-Type-Options: nosniff`.

### LOW-3 — `_run_export` swallows DB write failures silently

`export_routes.py:262-266`:

```python
try:
    db.update_data_export_request(request_id, status="failed", error=str(exc)[:500])
except Exception:
    pass
```

A storage-tier failure leaves the row stuck in `processing` forever. Users
hit the rate limit (HIGH-4) but never see a "failed" status to retry from.
Fix: at least log the swallowed exception; better, requeue with a backoff.

### LOW-4 — `_role_badge` etc imported lazily inside the request path

`export_routes.py:64-65` calls `_srv()._role_badge(user)` per request,
which goes through `sys.modules`. If `server.py` was reloaded mid-request
(dev only) the badge call can race. Cosmetic.

### INFO-1 — `EXPORT_TTL_DAYS = 7` constant duplicated

Both `export_routes.py:39` and `exports/generator.py:66` declare TTLs
(7 days vs `int(os.environ.get(...,7*86400))`). Centralise.

### INFO-2 — Test coverage for the canonical export builder, not the runtime one

`tests/test_data_export.py:362-422` enumerates 40+ expected keys in the
`_collect` bundle and asserts each is present. None of them exercise the
inline `_build_zip` in `export_routes.py` that *actually runs at runtime*.
Tests pass; live ZIPs ship minimal content. See HIGH-2.

### INFO-3 — README inside the ZIP and the README on disk diverge

`exports/generator.py:719-780` (`_readme`) is comprehensive and accurate.
`export_routes.py:140-158` writes a different, shorter README that
describes a different layout. Users get the second one. Cosmetic but
embarrassing in a regulator review.

---

## Tables NOT covered by the GDPR export bundle

Comparison: union of `(migrations/, db.py::SCHEMA)` CREATE TABLE statements
against `exports/generator.py:_collect()` *and* `export_routes.py:_build_zip()`.
Note: even the comprehensive `_collect()` is what tests measure — but it is
**not** what runs at request time (HIGH-2). The list below is tables with
user-scoped data that NO export path surfaces, regardless of which builder
runs.

Tables not exported (user-keyed, with personal data, never in the bundle):

- `analytics_events` — per-user activity stream (`db.py:368`); user-keyed but
  never written into the export ZIP
- `password_resets` — exclude is defensible, but the existence of past resets
  is Art. 15 data; not even a count is exported
- `enquiries` — pre-signup contact form; email-keyed; never exported
- `newsletter_subscribers` — email-keyed; never exported, never linked back
  to the user's bundle
- `impersonation_sessions`, `impersonation_actions` — when the user was the
  target of impersonation, Art. 15 says they should see it; not exported
- `incidents`, `incident_updates`, `status_subscriptions` — status-page
  notifications; `status_subscriptions` has `user_id` and is unexported
- `feature_flag_events` — flag-evaluation history per user; never exported
- `search_queries` — user search history; never exported (and would surface
  what they searched for, possibly sensitive)
- `engagement_prompt_dismissals` — never exported
- `bulk_fetch_counters` — API usage counters keyed to user; never exported
- `api_usage_hourly` — same; never exported
- `claude_usage_log` (with user_id added in migration 050+/051) — AI usage
  history per user; never exported
- `email_watermarks` — never exported (and contains a hash of the user's
  recipient email used to fingerprint leaked screenshots — Art. 15 data)
- `user_forensic_seeds`, `sentinel_predictions`, `watermark_seeds` — forensic
  fingerprints derived from this user's account; Art. 15 data; never exported
- `email_otps` (migration 019, 006) — never exported; small but real PII
- `two_fa_attempts` — never exported (a user has the right to see when their
  2FA was attempted, including from where)
- `security_events` — user-keyed security log; never exported (this is the
  *direct* Art. 15 right-of-access surface for security incidents)
- `realtime_connection_events` — per-user connection log; never exported
- `wallet_connect_nonces` — per-user nonces; small but not exported
- `user_accuracy` — derived from user predictions; never exported
- `external_forecasts` — user-uploaded forecasts; not in bundle
- `prediction_extractions`, `predictions_reextracted` — system-derived but
  user-attributable; never exported
- `share_metrics` — per-sharer metrics; never exported
- `shared_market_cards`, `shared_source_cards`, `shared_predictions` — the
  user is the `sharer_user_id`; never exported (the user's outbound share
  history is GDPR-relevant)
- `take_reports` — when the user reported a take or was reported on; never
  exported
- `take_resolution_runs` — never exported
- `churn_signals` — derived from this user's behaviour to predict churn;
  Art. 15 explicitly covers *profiling* outputs; never exported
- `processed_stripe_events` — per-customer Stripe webhook events; never
  exported (the user's billing event history is Art. 15 data)
- `subscription_pauses` — exported ✓ (in `_collect`), but **not** in the
  runtime `_build_zip`; same for everything below this point under HIGH-2
- `affiliate_links`, `affiliate_conversions` — partial coverage in `_collect`
  (`affiliate_accounts` is exported, conversions and links are not)
- `discord_servers` — admins of a server are users; the `setup_by_user_id`
  field is not exported under that user's bundle
- `slow_query_log`, `slow_request_log` — per-user-id-keyed traces; never
  exported (the latter has `user_id` on requests)
- `claude_kill_switch`, `claude_cost_alerts` — admin-scoped, no leak
- `feature_flags.updated_by_admin_id`, `email_templates.updated_by_admin_id`
  — admin-only audit pointers; the user is never the actor here

Tables EXPORTED ONLY by `exports/generator._collect()` (canonical) but NOT
by `export_routes.py:_build_zip()` (the one that runs at request time):

- `saved_predictions`, `followed_sources`, `user_topics`,
  `intelligence_conversations`, `intelligence_messages`,
  `user_market_alerts`, `user_sessions`, `user_bet_history`,
  `api_keys`, `telegram_user_links`, `email_unsubscribes`, `backtests`,
  `feedback_submissions`, `gifted_subscriptions`, `user_positions`,
  `user_predictions`, `user_prediction_stats`,
  `user_trading_addon_settings`, `user_market_credentials`,
  `polymarket_connections`, `kalshi_connections`, `whale_watchlist`,
  `notifications`, `notification_preferences`, `push_subscriptions`,
  `engagement_events`, `user_onboarding`, `user_first_week_goals`,
  `changelog_seen`, `market_takes`, `take_votes`, `user_follows`,
  `collections`, `collection_follows`, `saved_views`,
  `webhook_subscriptions`, `feedback_items`, `feedback_votes`,
  `feedback_comments`, `cancellation_attempts`, `subscription_pauses`,
  `user_invite_tokens`, `referrals`, `affiliate_accounts`,
  `weekly_reports`, `discord_user_connections`, `telegram_connections`,
  `embed_widgets`, `data_export_requests`, `audit_log` (target=user)

That second list is the **HIGH-2 manifestation**: until `_enqueue_export` is
fixed, every one of those tables is *promised by the README* and *covered by
tests* but **never actually surfaces in a real export ZIP**.

---

## Recommendations (priority order)

1. **Fix CRIT-2** (`export_routes.py:71-80`): delete the fallback secret,
   fail loud at startup if no signing key is configured.
2. **Fix HIGH-2** (`export_routes.py:269-271`): replace the inline
   thread-pool with a call to `exports.generator.generate()`, either
   directly or via ARQ enqueue.
3. **Fix CRIT-1 / HIGH-1** together: pick one deletion model, route both
   handlers through it, and have `process_scheduled_deletions` call
   `cascade_delete_user`.
4. **Fix HIGH-3**: gate the download endpoint behind a session cookie OR a
   single-use token. Audit-log every download.
5. **Fix HIGH-5**: route admin deletes through `cascade_delete_user` and
   record per-table rowcounts in the audit log.
6. **Fix MED-3**: extend the deletion sweep to `newsletter_subscribers` +
   `enquiries` matched by the pre-anonymisation email.
7. **Fix MED-4**: drop `email_send_log` or build it; either way the bundle
   needs to be honest about what it contains.
8. **Expand `_collect`** to cover the "Tables NOT covered" list above (or
   write a Custom Erasure Policy doc explaining why a given table is exempt
   — financial-record retention, fraud detection, etc.).
