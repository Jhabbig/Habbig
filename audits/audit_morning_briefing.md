# Morning Briefing Audit — `send_morning_briefings` (F7)

**Date:** 2026-05-15
**Scope:** Morning intelligence briefing email — opt-in plumbing, send-hour timezone correctness, PII surface in the rendered body, and unsubscribe link integrity.
**Module surface area inspected:**
- `gateway/migrations/013_morning_briefing.py` — schema (`users.morning_briefing_enabled`, `users.morning_briefing_hour`)
- `gateway/jobs/email_jobs.py::send_morning_briefings` (+ cron registration on line 548)
- `gateway/email_system/templates/morning_briefing.html`
- `gateway/email_system/unsubscribe.py` (`UnsubscribeManager`)
- `gateway/server_features.py::unsubscribe_page` (`/unsubscribe` handler)
- `gateway/email_system/service.py` (`_SUBJECTS`, `send_template`)
- `gateway/features.py` (`morning_briefing_email` kill-switch flag)
- `gateway/admin_routes.py` (admin template-editor registration)
- `gateway/admin_test_emails_routes.py` (test-send sample context)
- `gateway/tests/test_morning_briefing.py`

Severity legend:
- **HIGH** — exploitable today, compliance-breaking, or causes a user-visible failure on the unsubscribe path (CAN-SPAM/GDPR).
- **MED** — defence-in-depth gap, latent bug if surrounding code changes, or unclear posture.
- **LOW** — hygiene / informational.

## Severity counts

| Severity | Count |
|----------|------:|
| HIGH     | 3 |
| MED      | 4 |
| LOW      | 3 |
| **Total findings** | **10** |

---

## 1. Opt-in is respected

**Status:** Partial — opt-in column is enforced, but two related preferences are ignored.

The send loop pulls recipients with:

```sql
SELECT id, email, username FROM users
 WHERE morning_briefing_enabled = 1
   AND COALESCE(is_deleted, 0) = 0
   AND COALESCE(email_unsubscribed_at, 0) = 0
```
(`gateway/jobs/email_jobs.py:333-337`)

- **OK:** explicit opt-in via `morning_briefing_enabled = 1` (default `0`, per migration 013 line 18). No user is auto-enrolled.
- **OK:** soft-deleted users are filtered.
- **OK:** global unsubscribe (`email_unsubscribed_at`) is honoured.
- **OK:** users with `tier == "none"` are skipped inside the loop after the batched plan lookup (lines 442-444) — expired subscribers never receive the briefing.

### HIGH #1 — `email_digest = 0` is NOT honoured by morning briefings

The unsubscribe handler (`gateway/email_system/unsubscribe.py:74-78`) flips `users.email_digest = 0` when a user clicks the unsubscribe link with `scope = "digest"`. The weekly digest path filters on `COALESCE(u.email_digest, 1) = 1` (`email_jobs.py:154`), but the morning briefing query does **not**. A Pro user who unsubscribes from the weekly digest still receives the morning briefing every day — the user-visible intent ("stop the daily/weekly intelligence emails") does not match behaviour. Since the morning briefing template's own unsubscribe link is keyed to `type=digest` (see HIGH #3 below), this means clicking unsubscribe from the morning briefing flips `email_digest` but does **not** stop morning briefings. Combined, this is a regulator-visible failure of one-click unsubscribe under CAN-SPAM §316.5.

**Fix:** add `AND COALESCE(email_digest, 1) = 1` to the SELECT, OR introduce a dedicated `unsubscribed_from = "morning_briefing"` scope that flips `morning_briefing_enabled` to 0.

### MED #1 — No `morning_briefing_email` kill-switch enforcement

`gateway/features.py:69` registers `morning_briefing_email` in `KNOWN_FLAGS` with the comment `# daily-briefing email — kill switch`, but `send_morning_briefings` never calls `features.is_feature_enabled("morning_briefing_email", ...)`. The flag is dead — flipping it in the admin UI has no effect on the send. The weekly digest path has the same gap (`weekly_digest_email` flag, same shape) but at least the user-pref column there is honoured; morning briefing is doubly exposed.

### LOW #1 — Newsletter-marketing preference (`email_marketing`) not consulted

Defensible (the briefing is editorial, not marketing), but worth a comment in the SELECT explaining why `email_marketing` is intentionally ignored. A future maintainer is likely to assume parity with marketing emails and "fix" this in the wrong direction.

---

## 2. Send-hour timezone correctness

**Status:** Broken — the per-user "preferred hour" is dead schema; everyone receives at the same UTC instant regardless of their location.

### HIGH #2 — `morning_briefing_hour` column exists but is never read

Migration 013 adds `users.morning_briefing_hour INTEGER NOT NULL DEFAULT 8` (line 20) with the documented intent that Pro users can choose their delivery hour. The send job is registered as a single global cron:

```python
register_cron("send_morning_briefings", hour=8, minute=3)
```
(`gateway/jobs/email_jobs.py:548`)

`register_cron` in the jobs subsystem uses UTC. The SELECT (lines 333-337) does **not** filter on `morning_briefing_hour`, and the loop does not partition users by hour either. Effects:

1. Every opted-in user gets the email at 08:03 UTC — 4:03am EDT, 9:03am BST, 5:03pm NZST. The "morning" framing is wrong for ~80% of the timezone wheel.
2. The user-facing setting is misleading: changing `morning_briefing_hour` via a (currently non-existent) preference UI would have no visible effect, which will generate support tickets.
3. There is **no `users.timezone` / `users.user_tz` column anywhere in the schema** (verified by grep across `gateway/migrations/*.py`). Even if the cron were rewritten to fire hourly and filter on `morning_briefing_hour`, there is no way to interpret hour 8 in the user's local time. The hour is implicitly UTC.

**Fix path** (intentionally not pre-prescribing implementation since pre-release is off-limits):
- Add `users.timezone TEXT` (IANA tz, default `"UTC"`) in a new migration.
- Re-register the cron as hourly (`hour=*, minute=3`), filter the SELECT on `morning_briefing_hour = <current_user_local_hour>` derived from each user's tz.
- OR drop the schema and document "delivery at 08:03 UTC, period" — fine for a beta, but then remove the `morning_briefing_hour` column to avoid the lie.

### MED #2 — Daylight saving boundary is unhandled even at single-timezone resolution

The cron fires at a fixed UTC moment. On the two DST transition days per year, users in DST-observing regions will see the briefing arrive an hour earlier or later than the prior day. Acceptable for a beta but worth flagging if/when timezone support lands — the implementation must resolve "local hour 8" through a tz-aware library (`zoneinfo`) rather than naive offset math.

### MED #3 — `date` is computed at job-start, not per-user

Line 507: `"date": date.today().strftime("%B %d, %Y")`. `date.today()` is server-local (UTC on Cloudflare/most VPS). A user in UTC-08 receiving the email at 00:03 their local time will see the *next* day's date in the subject header — a small but real consistency hit for anyone reading the email at a US west-coast bedtime. Coupled with HIGH #2 this is mostly latent today.

---

## 3. PII in the email body

**Status:** Acceptable for the recipient themselves, but the design leaks intent if the email is forwarded.

The rendered context (`gateway/jobs/email_jobs.py:504-525`) contains:

| Key | Source | PII class |
|---|---|---|
| `display_name` | `username` or `email.split("@")[0]` | LOW: local-part of recipient's own email if no username |
| `date` | server-local date | — |
| `app_url` | env var | — |
| `top_edge_markets[*]` | public market data (title, prices, edge, source count) | — |
| `new_signals[*]` | `source_handle`, truncated `content`, `credibility` | — (public source data) |
| `approaching_resolutions[*]` | public market data | — |
| `subproduct_labels_str` | user's active subproduct list | MED: reveals subscription mix |
| `unsubscribe_url` | per-user signed token (in theory; see HIGH #3) | low (signed) |
| `watermark` | 6-char hex; visible in footer as `id:xxxxxx` | LOW: maps to user_id only via admin trace table |
| `watermark_zw` | invisible zero-width run | LOW: same property |

### MED #4 — `subproduct_labels_str` exposes the recipient's paid subproduct list

Non-Pro recipients see `Your briefing for: Crypto Edge, Sports` (template line 22). If the email is forwarded, screen-shared, or recovered from a compromised mailbox, it reveals which paid products this user owns. This is a small commercial-tier leak (a competitor can profile narve.ai's tier mix from leaked inboxes). Pro recipients have an empty list, so they're unaffected.

Not a CAN-SPAM/GDPR issue, but worth weighing: the same information is already visible to the user inside the app, and the unsubscribe link below the section already binds the email to a recipient. Suggest leaving as-is and documenting the leak in the template's header comment.

### LOW #2 — Email local-part falls into `display_name` when username is null

Line 508: `"display_name": user["username"] or user["email"].split("@")[0]`. If `username` is null/empty, the local-part of the email address is rendered as the salutation. This is the recipient's *own* email so the disclosure is to themselves, but a forwarded email then shows e.g. `Good morning, jane.smith` to a third party.

**Fix:** fall back to a generic `"Trader"` / `"there"` rather than the email local-part.

### LOW #3 — No raw email address in body (good)

Verified: the body never embeds `user["email"]` as a string. Header `To:` is the only recipient-address surface.

---

## 4. Unsubscribe link

**Status:** Broken — the morning briefing emits an unsigned URL that lands on the "Link expired or invalid" page.

### HIGH #3 — Morning briefing unsubscribe URL is missing the signed token

In `send_morning_briefings` (`gateway/jobs/email_jobs.py:524`):

```python
"unsubscribe_url": f"{app_url}/unsubscribe?type=digest",
```

The handler at `/unsubscribe` (`gateway/server_features.py:97-126`) reads `token` from the query string. Without a token, `UnsubscribeManager.unsubscribe(token)` returns `None`, and the handler renders **"Link expired or invalid. If you keep receiving emails, contact support."** (line 124).

Compare to the weekly digest, which uses the helper correctly (`email_jobs.py:289`):

```python
"unsubscribe_url": _unsub_url(u["id"], u["email"], "digest"),
```

That helper (lines 19-24 of the same file) wraps `UnsubscribeManager.get_unsubscribe_url(...)` which returns a properly signed `{base}/unsubscribe?token={token}&type={scope}` URL.

**Impact:**
1. **CAN-SPAM §316.5 violation (US):** the FTC requires "a clearly and conspicuously displayed return e-mail address or other Internet-based mechanism that a recipient may use to submit a request not to receive future commercial electronic mail messages." A link that always lands on "Link expired or invalid" does not satisfy this — and since HIGH #1 above already breaks the alternate path (clicking unsubscribe flips `email_digest = 0`, which is not consulted), users *cannot* unsubscribe from morning briefings end-to-end.
2. **GDPR Art. 21 / ePrivacy Art. 13 (EU):** the right-to-object surface is the unsubscribe link; an unsigned dead link is "no mechanism at all."
3. **Reputation/deliverability:** RFC 8058 List-Unsubscribe and the visible unsubscribe link being broken pushes recipients toward "mark as spam," which crashes domain reputation. Gmail's 2024 bulk-sender requirements (≥5000/day) make this a deliverability cliff, not a soft hint.

**Fix (one-line):** replace line 524 with `"unsubscribe_url": _unsub_url(user["id"], user["email"], "digest")` and pair it with the HIGH #1 fix so the click actually disables the morning briefing.

### MED #4 (cont.) — Once HIGH #3 is fixed, the scope name should be its own

Today both the weekly digest and the morning briefing share `scope = "digest"`. Clicking unsubscribe in the morning briefing should plausibly only stop the morning briefing, not also kill the weekly digest. Recommend a dedicated `"morning_briefing"` scope in `UnsubscribeManager.unsubscribe` that flips `morning_briefing_enabled = 0` (and only that). The shared scope is acceptable as long as both paths gate on the same column; today they don't.

### MED #5 — No `List-Unsubscribe` / `List-Unsubscribe-Post` header

`EmailService._send_via_relay` (`gateway/email_system/service.py:134-162`) does not emit `List-Unsubscribe` or `List-Unsubscribe-Post: List-Unsubscribe=One-Click` headers. Gmail and Yahoo bulk-sender enforcement (post-2024-02) treats this as a hard requirement for senders crossing 5000/day. Same gap on `_send_via_smtp`. This is a system-wide email-service issue not specific to morning briefing, but the morning briefing pushes daily volume.

### LOW #4 — No `_SUBJECTS["morning_briefing"]` mapping

`gateway/email_system/service.py:191-220` registers subject lines per template; `morning_briefing` is absent. `send_template` falls through to `_SUBJECTS.get(template, "narve.ai")` (line 118), so the morning briefing's subject is literally **"narve.ai"** — three identical-subject emails per week will quickly trip Gmail's threading and recipient blunting. Add an entry like `"morning_briefing": "narve.ai — Morning intelligence briefing"` (or better, include the date in the subject — but see MED #3 about the date-bug interaction).

---

## Summary of gaps (caller asked for "gaps")

Listed in fix-priority order:

1. **HIGH #3** — `unsubscribe_url` in `email_jobs.py:524` is unsigned; every morning-briefing unsubscribe link lands on "Link expired or invalid." End users cannot unsubscribe. CAN-SPAM/GDPR/Gmail bulk-sender exposure. **One-line fix.**
2. **HIGH #1** — `send_morning_briefings` SELECT does not honour `email_digest = 0`. A user who unsubscribed from the weekly digest still gets the daily briefing, and (combined with HIGH #3) cannot stop it. **One-line fix.**
3. **HIGH #2** — `users.morning_briefing_hour` is dead schema; cron is global at 08:03 UTC; no `users.timezone` column exists. Either wire it up or delete the column and document UTC-only delivery.
4. **MED #1** — `morning_briefing_email` kill-switch flag is registered but never evaluated. Either gate the job on `features.is_feature_enabled(...)` or drop the flag from `KNOWN_FLAGS`.
5. **MED #2** — Once tz support lands, resolve "local hour 8" via `zoneinfo`, not naive offset math (DST).
6. **MED #3** — `date` is computed once per job-run, not per-user; will show wrong day for users west of the server when HIGH #2 is fixed.
7. **MED #4** — `subproduct_labels_str` leaks tier mix in forwarded mail; document the trade-off.
8. **MED #5** — Email transport lacks `List-Unsubscribe` headers; system-wide deliverability risk amplified by morning briefing's daily cadence.
9. **LOW #1** — Add a comment in the SELECT clarifying why `email_marketing` is not consulted.
10. **LOW #2** — `display_name` falls back to email local-part; switch to a generic salutation.
11. **LOW #4** — No `_SUBJECTS["morning_briefing"]` entry; emails ship with subject "narve.ai".

## Cross-references

- Email-template-level findings on this same template are catalogued in `audits/audit_email_templates.md` (HIGH #2, MED #3 on tracking-pixel exposure).
- Watermark mechanics (PII-adjacent) are covered in `gateway/email_system/watermark.py` and previously audited; not duplicated here.
- The `_resolve_subproduct_filter` / `_resolve_batched` shape is shared with the weekly digest; any change to subproduct semantics must update both call sites.
