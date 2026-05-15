# Impersonation TTL & blocked-routes audit — 2026-05-15

Verifies three prior-audit assertions:

1. Impersonation cookie TTL ≤ 4h.
2. Active impersonation sessions auto-revoke on admin logout.
3. The blocked-routes list is exhaustive for destructive surfaces (no
   admin-impersonating-user can hit anything like `/admin/users/{uid}/delete`,
   `/profile/password`, `/api/embeds`, etc. while the session is active).

Scope: `gateway/`. Pre-release page (`gateway/static/prerelease.*`) is off
limits per task instructions. Synchronous bash only.

---

## 1. TTL ≤ 4h — PASS

Two sources of truth agree:

- `gateway/impersonation.py:32`
  `IMPERSONATION_COOKIE_TTL = 4 * 60 * 60` (4 hours)
- `gateway/server.py:255`
  `IMPERSONATION_COOKIE_TTL = 4 * 60 * 60` (duplicate constant; same value)

Enforcement points:

- Cookie `max_age=IMPERSONATION_COOKIE_TTL` set in
  `_set_impersonation_cookie` at `gateway/server.py:1689-1696`
  (`httponly=True`, `samesite="lax"`, `secure=IS_PRODUCTION`, path `/`).
- Server-side TTL ceiling enforced in `ImpersonationMiddleware.dispatch`
  at `gateway/server.py:1582-1589`:
  `if now - imp_row["started_at"] > IMPERSONATION_COOKIE_TTL:` →
  `end_impersonation_session(..., end_reason="expired")` and the cookie
  is cleared. Even a tampered-MaxAge cookie cannot extend past 4h because
  the check is `started_at`-based, not cookie-based.

**Soft note (not a gap):** the constant is duplicated in `impersonation.py`
and `server.py`. They're identical today, but a future edit that touches
one and forgets the other would silently desync. Consolidating to a
single import would harden this.

## 2. Auto-revoke on admin logout — PARTIAL FAIL

Trace through `gateway/server.py:4043-4085` (`@app.get("/logout")`):

- Deletes `sessions` row via `db.delete_session(token)` (4052).
- Revokes hardened session if present (4057-4061).
- Logs `ADMIN_LOGOUT` audit if admin (4063-4072).
- Clears hardened + pending-token + legacy session cookies (4073-4084).
- **Does NOT** call `_clear_impersonation_cookie(response, request)`.
- **Does NOT** call `db.end_impersonation_session(...)` for any of the
  admin's still-active impersonation_sessions rows.

Result: after logout the response carries no admin-session cookie, but
the browser still holds `narve_impersonation`. The next request hits
`ImpersonationMiddleware`, which discovers `admin_session_user is None`
(or a different user) and falls into the cross-check failure branch at
`server.py:1606-1624` — that branch DOES end the DB row (reason
`admin_session_mismatch`) and clear the cookie. So the session is
eventually neutered.

Two real problems with this indirect revoke path:

- **Audit-trail misclassification.** Every "admin logged out" termination
  is logged as `end_reason="admin_session_mismatch"` rather than
  `admin_logout`. The audit log can't distinguish a clean logout from a
  stolen-cookie replay, which is the single threat this reason code was
  invented for.
- **Window of stale state.** Between the moment logout returns and the
  next request, `impersonation_sessions` still shows `ended_at IS NULL`.
  The admin list at `/admin/impersonations` will show the session as
  Active. A second logged-in admin browsing the impersonation list would
  see a ghost session that no living cookie actually controls.

**Fix:** add to `/logout` (server.py:4043):

```python
# End any active impersonation sessions started by this admin and
# clear the impersonation cookie so the audit row reads admin_logout.
if user and user.get("is_admin"):
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT id FROM impersonation_sessions "
                "WHERE admin_user_id = ? AND ended_at IS NULL",
                (user["user_id"],),
            ).fetchall()
        for r in rows:
            db.end_impersonation_session(r["id"], end_reason="admin_logout")
    except Exception:
        pass
_clear_impersonation_cookie(response, request)
```

Helper to add to `queries/admin.py`: a bulk variant of
`end_impersonation_session` filtered by `admin_user_id` is cleaner than
the loop above, but the loop is fine given there will essentially never
be more than one active session per admin.

## 3. Blocked-routes exhaustiveness — PARTIAL FAIL

The asserted-blocked example from the prior audit — `/admin/users/{uid}/delete`
(`server.py:6237`) — IS blocked. It matches `/admin` in both
`_BLOCKED_PATTERNS` (state-changing) and `_READ_ALSO_BLOCKED_RE` (also
GETs/HEADs), per `impersonation.py:67` and `:96`. Verified by running
the live regex set against the path.

But the **assertion that blocks are exhaustive is wrong**. Sweeping every
`@app.(post|put|patch|delete)` decorator across `gateway/` and replaying
each path through the active `_BLOCKED_PATTERNS` / `_READ_ALSO_BLOCKED_PATTERNS`
regex list produced this set of destructive endpoints that are NOT
blocked while impersonating:

| Path | Method | File:line | Effect if executed while impersonating |
|---|---|---|---|
| `/profile/password` | POST | server.py:4846 | Changes the impersonated user's password. Direct account take-over: an admin can lock the user out. THIS IS THE MOST SERIOUS GAP. The blocklist has `/account/password` and `/settings/password` but the live route is `/profile/password`. |
| `/settings/disconnect/{source}` | POST | server.py:7418 | Disconnects polymarket/kalshi credentials AND calls `db.delete_user_positions(...)` — wipes the user's tracked positions for that platform. Destructive and irreversible for the user. |
| `/api/trading-addon/config` | PATCH | server.py:7532 | Overwrites the impersonated user's trading-addon config (kelly_fraction, etc.). User-visible behavioural change. |
| `/api/embeds` | POST | embed_routes.py:651 | Creates an embed widget under the impersonated user's account. The existing rule `/widgets` does NOT match `/api/embeds` — the live route changed/forked but the blocklist still names the old surface. |
| `/api/embeds/{widget_id}` | DELETE | embed_routes.py:704 | Deletes one of the user's widgets. |
| `/api/embeds/{widget_id}/rotate-token` | POST | embed_routes.py:713 | Rotates a widget token — invalidates any consumer relying on the old token. |
| `/api/share/market` | POST | routes_sharing.py:434 | Creates a share-card under user's identity. |
| `/api/share/source` | POST | routes_sharing.py:464 | Same. |
| `/api/share/prediction` | POST | routes_sharing.py:494 | Same. |
| `/api/saved/{prediction_id}` | POST/DELETE/PATCH | server_features.py:1011 / 1029 / 1109 | Mutates the impersonated user's saved-predictions list. |
| `/api/sources/{handle}/follow` | POST/DELETE | server_features.py:1158 / 1189 | Adds/removes a follow under the impersonated user. Pollutes their feed. |
| `/api/notifications/email-preferences` | POST | server_features.py:129 | Overwrites email-notification settings; can silently disable alerts the user actually wants. |
| `/api/leaderboard/participate` | POST/DELETE | routes_referrals.py:329 / 365 | Toggles leaderboard visibility for the user. |
| `/api/newsletter` | POST | server_features.py:408 | Subscribes the impersonated user to newsletter segments. |
| `/api/feedback` / `/api/feedback/{id}/vote` / `/api/feedback/{id}/comment` | POST | feedback_routes.py:524 / 614 / 685 | Posts feedback / votes / comments under the impersonated user's name. Public-facing impersonation. |
| `/api/invite/{code}/accept` | POST | routes_referrals.py:102 | Consumes an invite code on behalf of the user. |
| `/api/set-language` | POST | server_features.py:151 | Overwrites preferred_language. Low severity but still a write. |
| `/api/markets/{slug}/track-view` | POST | server_features.py:825 | Records analytics views as the impersonated user. Pollutes their analytics. |
| `/api/engagement/prompt/dismiss` | POST | engagement_routes.py:148 | Dismisses an engagement prompt the user would otherwise see. |
| `/api/status/unsubscribe` | POST | status_routes.py:318 | Unsubscribes user from status emails. |
| `/api/tools/card-preview` | POST | routes_sharing.py:354 | Generates a preview as the user — leaks into rate limits / quotas. |
| `/settings` | POST | server.py:7666 | Overwrites default_dashboard + env-impact prefs. Low severity. |
| `/subproduct-signup` | POST | subproduct_signup_routes.py:380 | Starts a subproduct signup flow under the user. Triggers downstream emails / Stripe paths. Should be blocked alongside `/billing` / `/subscribe`. |

**Severity ranking:**

- **Critical:** `/profile/password` — full account take-over. An
  impersonating admin can set a new password the real user does not know,
  then end the impersonation and log in directly as them.
- **High:** `/settings/disconnect/{source}` (deletes positions),
  `/api/embeds*` (creates / rotates / deletes widgets under user's
  identity), `/subproduct-signup` (financial flow).
- **Medium:** `/api/share/*`, `/api/feedback*`, `/api/saved/*`,
  `/api/sources/*/follow`, `/api/notifications/email-preferences`,
  `/api/trading-addon/config`. Public-facing or
  preference-mutating writes that impersonate the user's identity.
- **Low:** `/api/set-language`, `/api/markets/*/track-view`,
  `/api/leaderboard/participate`, `/api/newsletter`, `/api/invite/*/accept`,
  `/api/engagement/prompt/dismiss`, `/api/status/unsubscribe`, `/settings`,
  `/api/tools/card-preview`. Quality-of-life writes; still violate the
  "no side effects under the user's identity" intent.

**Suggested patch to `_BLOCKED_PATTERNS` in `gateway/impersonation.py`:**

```python
# Password / profile (audit 2026-05-15: live route is /profile/password,
# not /account/password — keep both rules so a future rename doesn't
# silently re-open the gap).
r"/profile/password",

# Settings — disconnect-market triggers delete_user_positions(), which
# is irreversible for the user.
r"/settings/disconnect/",

# Trading add-on (writes per-user trading config).
r"/api/trading-addon",

# Embed widgets — historic name was /widgets, the live surface is /embeds.
r"/api/embeds",
r"/embeds",

# Share-card writes (public artifacts attributable to the user).
r"/api/share/",

# Saved predictions / sources follow / notifications / newsletter /
# feedback / leaderboard / invite — all write under the impersonated
# user's identity.
r"/api/saved/",
r"/api/sources/.+/follow",
r"/api/notifications/",
r"/api/newsletter",
r"/api/feedback",
r"/api/leaderboard/participate",
r"/api/invite/.+/accept",
r"/api/set-language",
r"/api/markets/.+/track-view",
r"/api/engagement/",
r"/api/status/(un)?subscribe",
r"/api/tools/card-preview",
r"/settings",                 # catch-all for /settings POST
r"/subproduct-signup",
```

The `/settings` catch-all needs care: it also matches GET `/settings/...`
pages, but those pages render fine while impersonating. Only the
state-changing methods are checked once the rule lands in `_BLOCKED_PATTERNS`
(not `_READ_ALSO_BLOCKED_RE`), so the existing `_STATE_CHANGING_METHODS`
gate already preserves read access.

## 4. Stale assertion in the security-audit history

`gateway/server.py:1564-1566` says the cookie-replay vector was closed by
"migration 191 + queries/admin.py" at-rest token hashing.

- `gateway/migrations/191_impersonation_token_hash.py` **does not exist**.
  Last migration on disk is `188_fix_users_invite_token_fk.py`.
- `queries/admin.py:453` still stores `cookie_token` as the raw
  `secrets.token_urlsafe(48)` string and looks it up with
  `WHERE cookie_token = ?` (line 471). No HMAC, no SHA-256, no PBKDF2.

The cross-check at server.py:1606-1624 (admin's own narve_session must
resolve to `imp_row.admin_user_id`) is real and IS the actual cookie-
replay defence today. The stale comment misattributes that defence to a
hashing migration that was never written. The comment should be rewritten
or the hashing migration finally landed; the stolen-cookie threat is
genuinely mitigated by the cross-check alone, but a DB dump still reveals
every active impersonation token in plaintext.

---

## Gaps (summary, in priority order)

1. **CRITICAL** — `/profile/password` is reachable while impersonating
   and lets an admin change the impersonated user's password. Full
   account take-over with no user signal. (`gateway/impersonation.py`
   blocklist + `gateway/server.py:4846`.)
2. **HIGH** — `/settings/disconnect/{source}` deletes user positions
   while impersonating. (`gateway/server.py:7418`.)
3. **HIGH** — `/api/embeds*` widget surface is unblocked because the
   blocklist still references the obsolete `/widgets` name.
   (`gateway/embed_routes.py:651,704,713`.)
4. **HIGH** — `/subproduct-signup` is unblocked; sibling billing surfaces
   are blocked. (`gateway/subproduct_signup_routes.py:380`.)
5. **MEDIUM** — auto-revoke on admin logout is indirect. `/logout`
   doesn't end the impersonation row or clear the cookie; the next
   request's middleware does, but the audit reason is misleading
   (`admin_session_mismatch`) and there is a small window of stale state.
   (`gateway/server.py:4043-4085`.)
6. **MEDIUM** — `/api/trading-addon/config`, `/api/share/*`,
   `/api/saved/*`, `/api/sources/*/follow`, `/api/notifications/email-preferences`,
   `/api/feedback*` all let an impersonator write under the user's
   identity. (Scattered routes per table above.)
7. **LOW** — multiple preference / analytics endpoints listed in the
   table above; not security-critical but still violate "no side effects
   under the impersonated user".
8. **HOUSEKEEPING** — `IMPERSONATION_COOKIE_TTL` constant is duplicated
   in `impersonation.py:32` and `server.py:255`. Same value today; a
   one-sided edit would silently desync.
9. **HOUSEKEEPING** — `server.py:1564-1566` references a
   `migration 191` for at-rest token hashing that was never written; the
   `cookie_token` column is still stored plaintext at
   `queries/admin.py:453,471`. Comment should be corrected or migration
   landed.

## What is correct

- TTL = 4h, enforced both via cookie `max_age` and server-side `started_at`
  check. The server-side check cannot be tampered out from the client.
- The middleware cross-check (admin's own narve_session must resolve to
  the impersonation row's `admin_user_id`) IS the working cookie-replay
  defence and behaves correctly: stolen impersonation cookie used from
  a different admin / no admin → 302 to `/admin/users` and the DB row
  is ended with reason `admin_session_mismatch`.
- Banner injection, audit logging of `IMPERSONATION_START` /
  `IMPERSONATION_END` / `IMPERSONATION_BLOCKED`, and per-request
  `impersonation_actions` recording are intact (verified in
  `admin_routes.py:154-203` and `server.py:1652-1683`).
- `/admin/users/{uid}/delete` (the example from the prompt) IS blocked
  by the `/admin` regex in `_BLOCKED_PATTERNS` AND `_READ_ALSO_BLOCKED_RE`.
- `/admin/impersonations/end` IS correctly whitelisted via
  `_ALWAYS_ALLOWED` so the admin can always exit.

## Verification commands run

```
grep -n IMPERSONATION_COOKIE_TTL gateway/impersonation.py gateway/server.py
grep -nE '@app\.(post|put|patch|delete)' gateway/**/*.py
# Replay every destructive path through the live regex set:
python3 -c "<inline test of _BLOCKED_PATTERNS vs collected route list>"
grep -n end_impersonation_session gateway/server.py gateway/admin_routes.py gateway/queries/admin.py
grep -n /logout gateway/server.py
ls gateway/migrations | tail -5   # confirms migration 191 absent
```

No code was modified by this audit.
