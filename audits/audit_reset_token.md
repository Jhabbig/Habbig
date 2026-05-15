# Password Reset Token Audit

**Date:** 2026-05-15
**Scope:** Password reset token lifecycle — TTL, single-use, IP binding, revocation on password change.
**Hard rule observed:** synchronous bash only; pre-release routes **not** examined.

---

## Summary

Two reset code paths coexist:

| Path | Origin | TTL | Hash-at-rest | Single-use | IP recorded | IP enforced | Session kill |
|------|--------|-----|--------------|------------|-------------|-------------|--------------|
| `/forgot-password` + `/reset-password` (legacy form flow) | `gateway/server.py:3830, 5142, 5159` | **1h** (`RESET_TTL=3600`, `gateway/queries/auth.py:35`) | SHA-256 in `token_hash`, legacy `token` column also populated | Atomic `UPDATE … WHERE used=0` by row id | yes (`used_from_ip`) | no | yes — `DELETE FROM sessions` + `revoke_all_user_sessions` + bumps `jwt_invalidated_before` |
| `/auth/forgot-password` + `/auth/reset-password` (JSON flow, "Feature 2") | `gateway/server_features.py:233, 295` | **1h** (hard-coded `now + 3600`) | SHA-256 in `token_hash` | `UPDATE … SET used=1 WHERE id=?` (no `AND used=0` guard) | yes (`used_from_ip`) | no | partial — sessions DELETE + `jwt_invalidated_before`, no `revoke_all_user_sessions` |
| Token-bound legacy flow `POST /forgot-password` (invite-token + new password in one shot) | `gateway/server.py:3830` | n/a (no reset row, password swapped inline if invite token + email match) | n/a | n/a | n/a | n/a | yes — sessions deleted, `revoke_all_user_sessions` called |

TTL is **1 hour ≤ 24h ✓**. Hash-at-rest, single-use, and session revocation on completion are in place. **IP is recorded but never enforced.** **Outstanding reset tokens are not invalidated when the password is changed via `/profile/password` or by the inline `/forgot-password` invite-token flow.**

---

## Findings

### TTL — `gateway/queries/auth.py:35`, `gateway/server_features.py:270`
- `RESET_TTL = 60 * 60` (1 hour). Well under the 24h ceiling. ✓
- Both code paths agree on 1h. The JSON flow at `server_features.py:270` hard-codes `now + 3600` instead of importing `RESET_TTL`; non-load-bearing but drift-prone. **Gap (low):** duplicated constant.

### Single-use enforcement
- Legacy form: `server.py:5193` — `UPDATE password_resets SET used=1, used_from_ip=? WHERE id=? AND used=0` and `if cur.rowcount == 0: return "already used"`. Race-safe. ✓
- JSON flow: `server_features.py:339` — `UPDATE password_resets SET used=1, used_from_ip=? WHERE id=?` — **no `AND used=0` guard.** Practically the row is also gated by the prior SELECT (line 324) and the `used=0 AND invalidated=0` filter, but two concurrent submissions can both SELECT before either UPDATE commits. The password write (line 334) and the session DELETE (line 343) still run twice → wasted work, but the final user state converges. **Gap (low):** no atomic claim guard on this code path.
- DB-level guard at `gateway/queries/auth.py:490` (`use_password_reset`) is correctly atomic but unused by either live path.

### IP binding
- Both paths persist `used_from_ip` for the claimer's IP. ✓ (forensics)
- **Neither path binds the issuing IP to the redemption IP.** A token requested from one IP can be redeemed from any IP. The task notes IP binding is "optional but good"; current state = recorded only. **Gap (info):** no comparison of issuing vs redeeming IP; `password_resets` has no `issued_from_ip` column.

### Revocation on password change
- **Reset flow** (`/reset-password`, `server.py:5305-5316`): bumps `jwt_invalidated_before`, deletes all session rows, calls `revoke_all_user_sessions`. The reset row itself is marked `used=1`. ✓
- **Reset flow** (`/auth/reset-password`, `server_features.py:334-343`): bumps `jwt_invalidated_before`, deletes session rows. Does **not** call `revoke_all_user_sessions` — if `sessions` table and hardened-session store diverge, hardened cookies elsewhere may persist. **Gap (medium):** inconsistent session revocation between the two paths.
- **Profile password change** (`/profile/password`, `server.py:4943-4994`): rotates password, revokes hardened sessions, but does **not** invalidate any outstanding `password_resets` rows for the user, does **not** bump `jwt_invalidated_before`. A reset link in the user's inbox at the time of a voluntary change remains valid until its 1-hour TTL. **Gap (medium):** outstanding reset tokens survive a voluntary password change.
- **Invite-token forgot-password** (`server.py:3830-4019`): rotates password and kills sessions, but **also** does not invalidate outstanding `password_resets` rows or bump `jwt_invalidated_before`. **Gap (medium).**
- New reset request does **not** invalidate prior outstanding reset tokens for the same user. Multiple live reset tokens can coexist (each with its own 1h TTL). **Gap (low):** prior reset rows not marked `invalidated=1` on new request.

### Hash-at-rest
- Issuance: token is `secrets.token_urlsafe(36)` (legacy) / `(32)` (JSON), stored as both raw `token` (legacy compat) and `token_hash = sha256(raw)`. ✓
- Lookup: `_lookup_reset` (`server.py:5117`) checks `token_hash` first, falls back to plaintext `token`. Fine during rollover; legacy plaintext column should be dropped once no live links remain (none of the migration trail removes it through the current rev). **Gap (low):** legacy plaintext `token` column never removed.

### Garbage collection
- `purge_expired_resets()` is defined (`gateway/queries/auth.py:496`) and re-exported (`db.py:838`) but **never called** — no scheduler/cron registers it. Expired/used rows accumulate indefinitely. Not a security gap per se, but DB-bloat + IP-retention concern. **Gap (low):** orphan helper, no GC job.

### Rate limiting & enumeration
- Per-IP and per-email rate limits exist on both `/forgot-password` and `/auth/forgot-password` (3/h per email + 3/h per IP + 10/10min per IP); `/auth/reset-password` adds 5/h per IP on submission. Generic "if that account exists…" response prevents enumeration. ✓ (orthogonal to the requested scope; noted for completeness.)

---

## Gaps (prioritised)

1. **Outstanding reset tokens survive password changes via `/profile/password` and the invite-token-based `/forgot-password`.** Bump `jwt_invalidated_before` and `UPDATE password_resets SET invalidated=1 WHERE user_id=? AND used=0` from both handlers. *(medium)*
2. **`/auth/reset-password` does not call `revoke_all_user_sessions`** — divergence from `/reset-password`. Add the call. *(medium)*
3. **No issuing-IP binding** — `password_resets` has no `issued_from_ip` column; redemption IP is recorded only. Optional per spec; if added, compare on redemption and fall back to a softer warning rather than hard reject (corporate NAT, mobile network changes). *(info / optional)*
4. **New reset request leaves prior outstanding tokens live** — `UPDATE password_resets SET invalidated=1 WHERE user_id=? AND used=0` before inserting the new row. *(low)*
5. **`/auth/reset-password` UPDATE lacks `AND used=0` guard** — race-window double-execution wastes work; converges correctly but adds a SET `used=1 AND used=0` clause for symmetry with `server.py:5193`. *(low)*
6. **`purge_expired_resets()` never called** — wire into a daily scheduler job. *(low)*
7. **Legacy plaintext `token` column still populated and queried as fallback** — schedule a follow-up migration to drop the column and the legacy SELECT branch once the 1-hour rollover window is comfortably past. *(low)*
8. **`RESET_TTL` duplicated as a magic 3600 in `server_features.py:270`** — import from `queries.auth`. *(low)*

## Code references

- `gateway/queries/auth.py:35` — TTL constant
- `gateway/queries/auth.py:470-487` — `create_password_reset`
- `gateway/queries/auth.py:490-509` — `use_password_reset` / `purge_expired_resets` (unused)
- `gateway/server.py:3830-4019` — `/forgot-password` (invite-token inline-reset flow)
- `gateway/server.py:4943-4994` — `/profile/password` (authenticated change)
- `gateway/server.py:5117-5139` — `_lookup_reset`
- `gateway/server.py:5142-5156` — `GET /reset-password`
- `gateway/server.py:5159-5232` — `POST /reset-password`
- `gateway/server_features.py:229-286` — `_hash_reset_token` + `/auth/forgot-password`
- `gateway/server_features.py:295-345` — `/auth/reset-password`
- `gateway/migrations/003_password_reset_hardening.py` — added `used_from_ip`, `invalidated`, `token_hash`, `jwt_invalidated_before`
- `gateway/db.py:81-95` — `password_resets` schema + index
- `gateway/tests/test_password_reset.py`, `gateway/tests/e2e/test_password_reset_flow.py` — existing coverage
