# Adversarial audit — `gateway/queries/users.py`

Date: 2026-05-15
Reviewer: Claude (Opus 4.7)
Scope: GDPR-export completeness, GDPR-delete cascade correctness, admin-level enum bounds, impersonation safeguards.

## Preface — file does not exist

There is **no `gateway/queries/users.py`** in this repo. User-domain queries
live across:

- `gateway/queries/auth.py` — `create_user`, `set_user_role`, `set_user_admin`,
  `cascade_delete_user`, session lifecycle.
- `gateway/queries/admin.py` — impersonation-session CRUD, audit log, admin
  analytics.
- `gateway/queries/data_exports.py` — GDPR export-request rows.
- `gateway/exports/generator.py` — actual GDPR ZIP builder.
- `gateway/impersonation.py` — impersonation policy / blocked-path patterns.
- `gateway/admin_routes.py` + `gateway/server.py` — HTTP handlers that wire it
  together.

This audit reviews the user-data surface end-to-end across those files using
the four audit axes the brief asked for. **No code was changed.**

---

## Severity counts

| Severity | Count |
| --- | --- |
| CRITICAL | 1 |
| HIGH | 3 |
| MEDIUM | 5 |
| LOW | 4 |
| INFO | 3 |

**Top 3 (must-fix order):**

1. **CRITICAL — Admin delete endpoints bypass `cascade_delete_user`.** Both
   `/admin/users/{user_id}/delete` (`server.py:6187`) and the bulk
   `/admin/users/bulk` with `action="delete"` (`server.py:6248`) execute
   `DELETE FROM sessions / subscriptions / users` directly. Every other
   user-scoped table is left to FK CASCADE (if defined) or orphaned (if not).
   The single-row `db.cascade_delete_user(user_id)` already exists in
   `queries/auth.py:870` and is used by the user-initiated `/account/delete`
   path — but the admin delete paths reimplement a strict subset and never
   call it. Result: admin-driven GDPR Art. 17 deletions are **incomplete**
   relative to user-driven ones, in a way the audit log will not show.

2. **HIGH — `cascade_delete_user` only matches columns literally named
   `user_id`.** It enumerates `sqlite_master`, checks each table for a column
   named `user_id`, and deletes by it. Every user-FK column with another name
   is silently skipped: `follower_user_id`, `followed_user_id`,
   `owner_user_id` (collections), `referrer_user_id`, `referred_user_id`,
   `admin_user_id`/`target_user_id` (impersonation_sessions),
   `setup_by_user_id`, `used_by_user_id`, `referred_by_user_id`,
   `claimed_by_user_id`. Whether these rows survive depends entirely on the
   FK CASCADE the migration happened to add. Some are protected
   (`user_follows`, `collections`, `referrals` — `ON DELETE CASCADE`), but
   nothing in `cascade_delete_user` audits or enforces that. A migration that
   adds a new `*_user_id` column without `ON DELETE CASCADE` and without
   renaming it to `user_id` will silently leak personal data through GDPR
   deletion, with zero test coverage to catch it.

3. **HIGH — GDPR export bundle silently swallows missing tables.**
   `_safe_query` in `exports/generator.py:141` catches `OperationalError`
   for `no such table` / `no such column` and returns `[]`. That is
   *intentional* for schema drift, but it means a production-environment
   schema regression (e.g. a renamed column, dropped index that takes the
   table with it, migration that didn't run) produces an export ZIP that
   **looks complete** (the file is present with an empty CSV/JSON) and never
   raises. Combined with the lack of any row-count assertion against
   sample-account fixtures, an entire category of personal data can vanish
   from the export without a single warning in logs.

---

## Detailed findings

### A. GDPR export — `exports/generator.py::_collect`

#### A1. CRITICAL/HIGH — silent table fallthrough

(See top-3 item 3.) `_safe_query` returns `[]` on `no such table` or `no
such column`, with no log, no metric, no manifest annotation. Recommend at
minimum: log a WARNING with the table name; add a `manifest["warnings"]`
list in the ZIP so the data subject and ops can see drift.

#### A2. HIGH — schema-driven blind spots

The `_collect` bundle is hand-maintained per table. Tables added after
2026-05-14 that aren't appended to the function list **never appear** in any
export. Sampling against migrations 170+ vs. the bundle keys, the following
appear to be **not exported**:

- `engagement_events_2026` (if any post-187 rename) — present today, but no
  CI guard.
- `user_market_alerts_dispatched` (migration 174) — dispatch history.
- `user_predictions_resolution_log` — if resolution side-effects are tracked.
- Several social-graph tables added late: `feedback_subscriptions`,
  `notification_dispatch_log`.
- Any whale-dashboard cross-db data beyond `whale_watchlist` (positions,
  filings_seen).

This is by inspection only — there is no CI test that compares
`bundle` keys against `SELECT name FROM sqlite_master WHERE type='table' AND
EXISTS (column user_id)`. Recommend a unit test that fails when a
user-FK-bearing table is added without a corresponding bundle entry.

#### A3. MEDIUM — `user_follows` only covers two FK columns by name

`bundle["user_follows"]` queries `follower_user_id = ? OR followed_user_id =
?`. Good. But the **inverse** asymmetry: `_scrub_audit_row` strips
`admin_email`, which is correct for the user's view of admin actions, but
the export does *not* include audit rows where `admin_user_id = ?` (i.e.
the data subject is themselves an admin — what *they* did to other users
is missing from their own data). For a normal user this is a no-op; for an
admin requesting their own export it is an Art. 15 gap.

#### A4. MEDIUM — `webhook_subscriptions` redaction is column-level only

`bundle["webhook_subscriptions"]` SELECTs a hand-rolled column list that
omits `signing_secret` / `secret`. Good. But the README says
"Webhook signing secrets" are redacted — there is no assertion that the
SELECT list stays in sync if a future migration adds a new secret column
(`shared_key`, `hmac_pepper`). Recommend either `SELECT *` followed by an
explicit drop-list, or a unit test that introspects the table and fails
on any column matching `/secret|key|token|wallet/i` that isn't whitelisted.

#### A5. MEDIUM — sessions export filters by allow-list, but `pending_totp_secret` is in `sessions`, not `user_sessions`

`bundle["sessions"]` reads from `user_sessions` (hardened). The legacy
`sessions` table — which has `pending_totp_secret`,
`pending_totp_secret_at`, `csrf_token` — is **not exported at all**. Net
effect for the user: their login history is correct (hardened table is
the source of truth), but if the legacy table holds rows the user could
argue under Art. 15 they should see at least the timestamps. Risk is
low; sensitivity of the missing rows is also low (no IP, no UA in
legacy). Recommend either documenting the omission in README.txt or
adding a redacted echo from `sessions`.

#### A6. LOW — `data_export_requests.file_path` not included by design

The export omits `file_path` and `download_url` from the
`data_export_requests` bundle entry. Correct: those are internal storage
locations and signed URLs that could be replayed. Worth a one-line
README mention.

#### A7. LOW — `audit_log` capped at 1000 rows

Hard-coded `LIMIT 1000` on the audit-log echo. For a heavy-use account
(years of admin notes), the user only sees the most recent 1000 entries
with no pagination or warning. Recommend either no limit (audit log is
small enough) or a `truncated: true` flag in the manifest.

#### A8. INFO — newer connection tables exported but with `source` injection

`polymarket_connections` and `kalshi_connections` are merged with `source`
synthetically added via `{**r, "source": "polymarket"}`. Works, but
fragile if a future migration adds a real `source` column with different
semantics. Belt-and-braces: alias in SQL.

---

### B. GDPR delete — `queries/auth.py::cascade_delete_user`

#### B1. CRITICAL — admin paths bypass the cascade

See top-3 item 1. The user self-serve path `/account/delete` correctly
calls `db.cascade_delete_user`. Both admin paths reimplement a 3-table
delete. Reproduction:

```
# server.py:6201-6204 (admin_delete_user)
with db.conn() as c:
    c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    c.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
```

```
# server.py:6248-6254 (bulk delete)
elif action == "delete" and (admin.get("admin_level") or 0) >= 2:
    target = db.get_user_by_id(uid)
    if target and (target["is_admin"] or 0) < 2:
        with db.conn() as c:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            c.execute("DELETE FROM subscriptions WHERE user_id = ?", (uid,))
            c.execute("DELETE FROM users WHERE id = ?", (uid,))
```

Because `PRAGMA foreign_keys = ON` is set (`db.py:261`), FKs declared as
`ON DELETE CASCADE` will still propagate from the final `DELETE FROM
users`. The functional difference vs. `cascade_delete_user` is therefore
*only* the tables that have a `user_id` column **with no FK constraint
at all**, which exist in this schema (any analytics-style table created
before the FK convention was enforced, e.g. `analytics_events`,
`feature_flag_events`, `login_failures`, `rate_limits`, the older
`email_send_log` rows). Those rows survive admin deletion silently — a
direct GDPR Art. 17 breach for admin-driven deletion.

Fix: have both admin endpoints call `db.cascade_delete_user(user_id)`
and use the returned dict for the audit log so the scope of every
deletion is recorded.

#### B2. HIGH — `cascade_delete_user` only matches `user_id`

See top-3 item 2. The function scans every table and only DELETEs where
`'user_id' in cols`. It misses:

| Column name | Tables affected (sample) | FK CASCADE? | Net |
| --- | --- | --- | --- |
| `follower_user_id`, `followed_user_id` | `user_follows` | yes | safe |
| `owner_user_id` | `collections` | yes | safe |
| `referrer_user_id`, `referred_user_id` | `referrals` | yes | safe |
| `admin_user_id`, `target_user_id` | `impersonation_sessions`, `impersonation_actions` | yes (via FK to users) | safe for target; **admin's own deletion would CASCADE-drop sessions where they were the actor**, destroying audit history |
| `setup_by_user_id` | feature flags audit | SET NULL | safe |
| `used_by_user_id`, `claimed_by_user_id` | `invite_tokens`, `user_invite_tokens` | varies | mixed |
| `referred_by_user_id` | `users` (self-ref) | SET NULL | safe |
| `recipient_user_id` (if any), `gifter_user_id` | gifted_subscriptions | check | unknown |

The `impersonation_*` cascade is a **distinct concern**: deleting an
admin user via `cascade_delete_user` cascades through every
`impersonation_sessions.admin_user_id` row they ever created, wiping the
audit trail of impersonation events against *other* users. Recommended
fix: change those FKs to `ON DELETE SET NULL` so the audit log
survives, or have `cascade_delete_user` refuse admins and require a
super-admin to demote them first.

#### B3. MEDIUM — `cascade_delete_user` per-table try/except hides FK failures

```python
try:
    cur = c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    ...
except Exception:
    continue
```

If a table has `ON DELETE RESTRICT` (none today, but a future migration
could add one), the inner DELETE would raise, get caught, and silently
skipped. The outer `DELETE FROM users` then either succeeds (FK was on
the *user* side) or fails (FK pointed *at* users — also caught higher
up). Recommend logging the exception and a metric increment so failures
don't disappear.

#### B4. MEDIUM — `cascade_delete_user` is not transactional

The function uses one `with db.conn() as c:` block, but SQLite + python
`with` semantics commit on context exit. A crash mid-iteration leaves
the database in a half-deleted state, and the function offers no
rollback. Given the loop can DELETE from 30+ tables, this is a real
window. Recommend wrapping in an explicit `BEGIN; ... COMMIT;` or using
`c.execute("SAVEPOINT cascade_delete"); ...; c.execute("RELEASE
cascade_delete")` with rollback on exception.

#### B5. MEDIUM — `cascade_delete_user` does not delete the export ZIPs

`/account/delete` calls `cascade_delete_user` which deletes the
`data_export_requests` row but **does not delete the on-disk ZIP** in
`EXPORT_DIR`. The signed URL is invalidated by row deletion, but the
file persists until `EXPORT_TTL_SECONDS` (7 days) or manual GC. A user
exercising Right-to-Erasure expects on-disk artifacts to go with them.
Recommend `for row in db.list_user_data_exports(user_id):
unlink(row["file_path"])` before the row delete.

#### B6. LOW — `account/delete` rate limit is 3/hour but bypasses lockout

`_is_rate_limited("account-delete:user_id", 3, 3600)` — fine. But it's
keyed per user_id, so a compromised session can issue 3 deletes per
hour. The flow requires re-typing password (`verify_password`), which
mitigates session hijack — but there is no separate brute-force lockout
on the password re-prompt. A determined attacker with a session cookie
and partial password knowledge can fail-password 3 times per hour from
each user/ip pair. Recommend hooking `record_login_failure` /
`is_login_locked` into the password re-prompt.

#### B7. LOW — `delete_sessions_for_user` in `auth.py` deletes from legacy `sessions` only

`cascade_delete_user` skips the hardened `user_sessions` table because
its column is `user_id` and the table is `user_sessions` (not
`users_sessions` etc.) — wait, this **is** caught (column is named
`user_id`). However the function's docstring says "every row in any
table that has a user_id column", and the loop indeed does, but it does
not call `revoke_all_user_sessions` (which sets `revoked = 1`). DELETE
is harsher than revoke; functionally equivalent for the user's own
deletion, but it means `user_sessions.revoked_at` audit history is lost.
Acceptable for GDPR but worth a comment.

#### B8. INFO — `cascade_delete_user` returns the count dict, but neither admin path uses it

The function returns `dict[str, int]` of `{table: rows_deleted}` for
audit. The user self-serve path logs it (`log.info("account.delete: ...
cascade=%s", deleted)`). If the admin paths are fixed (B1), they should
pass this dict into the audit log entry's `notes` field.

---

### C. Admin-level enum bounds — `set_user_role`, `create_user`

#### C1. MEDIUM — `create_user(admin_level=3)` is accepted

```python
def create_user(email, password, username="", is_admin=False, admin_level=0):
    ...
    level = admin_level if admin_level else (1 if is_admin else 0)
    ...
    c.execute("INSERT INTO users (... is_admin) VALUES (..., ?)", (..., level))
```

There is **no upper bound** on `admin_level`. A caller (today: tests,
fixtures, future admin self-register code) can pass `admin_level=3` and
it will be persisted as `users.is_admin = 3`. All downstream code uses
`>= 1` (any-admin) and `>= 2` (super) — so a value of 3 is effectively
indistinguishable from 2 for *gating*. But:

- `_users_role_label` (`admin_routes.py:2003`) returns "Super admin" for
  any `level >= 2`. A `level=3` user looks identical in the admin UI.
- `_can_manage_user` returns True for `caller_level >= 2`, so a
  `level=3` user can manage everyone (including `level=2`s).
- Impersonation check `target_level >= admin_level` (`admin_routes.py:133`)
  — a `level=3` admin could impersonate a `level=2` super admin, which
  the comment says is forbidden ("privilege laundering"). **This is a
  privilege-escalation primitive** if `level=3` ever lands in production.

The smuggling vector is narrow today (no public endpoint accepts an
arbitrary `admin_level`), but `create_user` is exposed via the admin
"create user" flow and via `db.create_user` from any future migration
script or test bleed. Recommend clamping in `create_user` and
`set_user_role` to `level in (0, 1, 2)`. The HTTP endpoint
`/admin/users/{user_id}/role` already clamps (`server.py:6025`) —
defense-in-depth says the DB-layer function should clamp too.

#### C2. MEDIUM — `set_user_role` accepts any int and stores it raw

```python
def set_user_role(user_id: int, level: int) -> None:
    ...
    c.execute("UPDATE users SET is_admin = ? WHERE id = ?", (level, user_id))
```

Same issue as C1 but at the UPDATE path. The only caller that validates
is `admin_set_role` in `server.py:6025`, which checks `0 <= level <= 2`.
Direct calls from `set_user_admin` (passes 0/1, safe) and tests
(`test_admin_users.py`) are also safe. But any future caller (CLI
script, migration) could pass an arbitrary int. Recommend the same
clamp inside `set_user_role`.

#### C3. LOW — `set_user_admin(user_id, True)` always stores 1, not 2

The legacy `set_user_admin` is wired through `/admin/users/{user_id}/promote`
and the bulk action. Both promote to `level = 1` (regular admin),
never to `2`. Good. But the function silently demotes a `level=2`
super admin to `level=1` if `set_user_admin(uid, True)` is called on
them — there's no read-modify-write guard. Today `_can_manage_user`
blocks a `level=1` admin from acting on a `level=2`, but a `level=2`
calling `set_user_admin(other_level_2, True)` would *demote* the
target. Bulk action with `action="promote"` does not preserve existing
super-admin status. Recommend a no-op when the current level >= the
target level being set.

#### C4. LOW — `is_admin` column is `INTEGER` with no `CHECK` constraint

The migration that added the level enum (around `024_admin_features.py`
or similar) keeps `is_admin INTEGER`. A `CHECK (is_admin IN (0, 1, 2))`
would belt-and-braces the DB layer against C1/C2 even if a function
forgets to clamp.

#### C5. INFO — `subproduct_access.py:45` documents the convention

The comment in `subproduct_access.py:45` ("`users.is_admin == 2` is
super-admin, 1 is regular admin, 0 is user") is the only place the
enum is explicitly enumerated. Move this into a constants module
(`auth/constants.py:AdminLevel.SUPER = 2`) so every gate uses the
named constant instead of magic `>= 2` literals scattered across 40+
files.

---

### D. Impersonation safeguards — `impersonation.py`, `admin_routes.py`,
   `server.py::ImpersonationMiddleware`

#### D1. HIGH — Live impersonation survives admin de-privilege

The middleware (`server.py:1503`) re-fetches the admin row each request
but **does not check `admin_row["is_admin"]`**. If a super-admin demotes
the impersonating admin to `is_admin = 0` mid-session, the
impersonation cookie remains valid until the 4h TTL or the admin's own
session is revoked. The admin can continue acting as the target user
even after their admin privileges were stripped. Recommend: in the
middleware, after the admin/target lookups, if `admin_row["is_admin"]
< 1` end the impersonation session and clear the cookie.

#### D2. MEDIUM — `is_action_blocked` uses `re.search`, not `fullmatch`

Pros: catches `/billing/foo`, `/admin/anything`. Cons: false positives
if a path *contains* a blocked substring elsewhere
(`/api/widgets-public-info` would be blocked by `r"/widgets"`). Today
the patterns are coarse enough that the false-positive surface looks
small, but a future admin-readable route at `/admin/impersonations/.../view`
is already on the allow-block boundary — it would be blocked because
`r"/admin"` matches. The `_ALWAYS_ALLOWED = {"/admin/impersonations/end"}`
allow-list is exact-match only, so any reasonable admin route under
`/admin/impersonations/{id}/notes` is blocked during impersonation
(which is intentional but worth surfacing in tests).

#### D3. MEDIUM — Cookie is `httponly` + `samesite=lax` but no IP binding

The impersonation cookie is set with `httponly=True`, `samesite="lax"`,
`secure=IS_PRODUCTION`, `max_age=14400`. Token is `secrets.token_urlsafe(48)`
— good entropy. There is **no IP binding** — if an admin's laptop is
stolen mid-session, the thief has 4 hours of impersonation power. The
session lookup is purely by cookie token. Recommend recording the
admin's IP at start and rejecting requests whose IP doesn't match
(with a soft fallback for IP changes on mobile/VPN).

#### D4. MEDIUM — `impersonate_start` doesn't verify the admin's session was 2FA-confirmed

`admin_routes.py:113` checks `admin_level >= 1` and `target_level <
admin_level`. It does NOT check `session_two_fa_verified` or any
"fresh authentication" timer. An admin whose session is hours/days
old and was never re-verified can start impersonation. Recommend
requiring `session_two_fa_verified(session_token)` AND a fresh
verification within (say) 10 minutes before allowing `impersonate_start`.

#### D5. MEDIUM — Bulk delete (`server.py:6248`) has weaker enum guard

`action == "delete" and (admin.get("admin_level") or 0) >= 2` —
correctly requires super-admin. BUT the per-row inner guard is
`target["is_admin"] or 0) < 2`, which only blocks deleting other
**super** admins. A super admin can delete a regular admin (level 1)
which feels intentional. However, the singleton `admin_delete_user`
endpoint at line 6196 also blocks `target_level >= 2` — symmetric.
This means **two super admins can delete each other** as long as one
moves first. Recommend requiring super admins to demote each other
to `level=1` before deletion is allowed.

#### D6. LOW — `record_impersonation_action` is best-effort

Wrapped in `try/except: pass`. If the audit insert fails (disk full,
table locked) the request still proceeds. For an audit log this is
**unsafe-default** — recommend a synchronous fail-closed in production
mode (env-gated). At minimum, increment a Prometheus counter so
silent audit drops are visible.

#### D7. LOW — `display_name_for` exposes target email in banner

The banner HTML embeds the impersonated user's full email
(`username (email)`). If the admin screen-shares (e.g. a support
call), the email is plaintext and unmaskable. Recommend a partial
mask (`u***@gmail.com`) via `db.mask_email` which already exists in
`queries/auth.py:444`.

#### D8. INFO — `_BLOCKED_PATTERNS` is good but lacks coverage for the new endpoints

The block list does not include:
- `/api/v\d+/account/delete` (only `/account/delete` and
  unversioned `/api/...` are covered).
- `/api/intelligence/conversations/.+/delete` — covered by
  `r"/api/intelligence"` because `search` is substring-based, but
  fragile.
- `/integrations/(polymarket|kalshi)/.+/disconnect` — disconnecting
  the user's brokerage during impersonation is not on the list.
- `/settings/notifications` POST — admin could toggle alerts on the
  user's behalf, polluting their notification preferences.

Recommend an explicit allowlist-style mode where, during impersonation,
ONLY GET/HEAD/OPTIONS pass through by default and POST requires an
explicit per-route allowlist entry.

---

## Cross-cutting observations

- **No unit tests directly exercise `cascade_delete_user`'s table
  coverage** vs. `sqlite_master`. Recommend a CI test that creates a
  user, inserts a row into every user-FK table, calls
  `cascade_delete_user`, then asserts zero remaining rows referencing
  the user — discovering both unscoped-by-name and uncascaded tables.

- **No CI guard ties `exports/generator._collect` bundle keys to the
  set of `user_id`-bearing tables.** The same shape of test (snapshot
  the bundle keys, snapshot the schema, fail on drift) would catch A2.

- The admin role enum (`AdminLevel`) is informal — no `IntEnum`, no
  shared constant. Recommend `auth/constants.py::AdminLevel` and
  global migration to it.

- `cascade_delete_user`, `create_user`, and `set_user_role` should
  live in a `queries/users.py` module (the file the audit brief asks
  about). Today they're in `queries/auth.py`, which conflates session
  auth with user lifecycle.

---

## Severity rubric

- **CRITICAL** — exploitable today, leaks personal data, or violates
  a GDPR right.
- **HIGH** — silent failure mode that a future migration / refactor can
  trip with no test catching it.
- **MEDIUM** — correctness gap or defence-in-depth weakness.
- **LOW** — cosmetic, ergonomic, or low-likelihood.
- **INFO** — convention / hygiene.
