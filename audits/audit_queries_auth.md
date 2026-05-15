# Adversarial Audit — `gateway/queries/auth.py`

Date: 2026-05-15
Auditor: Claude (Opus 4.7)
Target: `/Users/shocakarel/Habbig/gateway/queries/auth.py`
Supporting layer reviewed: `/Users/shocakarel/Habbig/gateway/db.py` (schema for
`sessions`, `password_resets`, `user_sessions`), `/Users/shocakarel/Habbig/gateway/server.py`
(reset/forgot routes, in-process lockout), `/Users/shocakarel/Habbig/gateway/server_features.py`
(`/auth/login` handler), `/Users/shocakarel/Habbig/gateway/migrations/003_password_reset_hardening.py`,
`/Users/shocakarel/Habbig/gateway/migrations/007_user_sessions_hardening.py`.

Scope tightly bounded to the six attacker classes named in the brief:

1. PBKDF2 iteration count (≥600k)
2. Salt length (≥16 bytes)
3. Constant-time hash compare (`hmac.compare_digest`)
4. Session-token hashing before DB store
5. Password-reset token entropy
6. Login rate-limit per email

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 1 |
| Medium   | 3 |
| Low      | 3 |
| Info     | 2 |
| **Total**| **9** |

## Top 3 findings (ranked by exploitability × impact)

1. **HIGH-1** — Legacy `sessions` table stores raw session tokens as PRIMARY KEY
   plaintext (`auth.py:220-227`, schema `db.py:36-44`). A read-only DB compromise
   (backup leak, SQLi exfil of a single table, a logging mishap) instantly yields
   *usable* session cookies for every signed-in user. The hardened `user_sessions`
   table next to it does the right thing (SHA-256 hash at rest), and BOTH tables
   are written on login — so the legacy table is a live, undefended duplicate of
   credentials that the hardened path was explicitly designed to protect. (See HIGH-1.)
2. **MED-1** — Per-email login rate limit is too loose and is evaded by reset.
   `/auth/login` caps at 5 wrong attempts per 10 min per email (`server_features.py:1738`),
   but the per-process `_is_account_locked` (`server.py:1803`) and `is_login_locked`
   in this module (`auth.py:88`) are *not wired into the JSON login path at all*.
   Net effect: an attacker gets ~30 password guesses/hr/email against a single
   process; behind multiple workers / Redis-less fallback the windows are
   per-process so the effective cap is `5 × N_workers` per 10 min. No long-term
   ceiling (the 30-failures-in-24h cap exists only in the dead in-memory store).
   (See MED-1.)
3. **MED-2** — `password_resets.token` still accepts the raw plaintext token
   (`auth.py:474-483`, fallback branch in `server.py:5086-5089`). The hash column
   was added in migration 003 and `create_password_reset` writes BOTH (`auth.py:466-470`),
   but the plaintext column is still UNIQUE-indexed and queried. A DB leak of
   `password_resets` therefore hands an attacker a live reset link for any user
   whose token is within the 1-hour TTL, plus permanent ability to recover any
   past raw token (the comment says rows are dropped on use, but `purge_expired_resets`
   only fires on demand and `use_password_reset` flips `used=1` rather than
   nulling the raw column). (See MED-2.)

---

## Findings

### HIGH-1 — Legacy `sessions.token` stored as plaintext (PRIMARY KEY)

**Where.** `auth.py:220-227` (`create_session`), `auth.py:239-249` (`get_session`),
schema `db.py:36-44`.

```python
def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(48)
    ...
    c.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, now, now + SESSION_TTL),
    )
```

```sql
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    ...
);
```

**Why it matters.** The 90-day TTL (`SESSION_TTL`) means at any moment a snapshot
of `sessions` contains long-lived usable cookies for the entire signed-in user
base. The hardened `user_sessions` table sitting next to it (`auth.py:765-817`)
stores `token_hash = SHA-256(raw)` exactly to defeat this — and login writes to
BOTH tables in the same transaction (migration 007 comment confirms intent). The
mitigation is half-applied: the hardened table is hashed, the legacy one is not,
and the legacy table is *still authoritative* for CSRF and 2FA state lookups
(`set_session_csrf`, `get_session_csrf`, `mark_session_two_fa_verified`,
`session_two_fa_verified` all key off the raw token). So an attacker reading
the legacy table doesn't just get a stale duplicate — they get the CSRF token
material as a bonus.

Attack scenarios that read the DB without RCE: SQLite file copy via a
misconfigured backup, an unrelated SQLi gadget in any query, a read-only
admin DB browser left exposed, log accidents.

**Recommendation.** Mirror the hardening from migration 007 onto `sessions`:
add a `token_hash` column, store SHA-256 of the raw token, hash on every read
through `get_session` / `set_session_csrf` / `get_session_csrf` /
`mark_session_two_fa_verified` / `session_two_fa_verified`, then null out the
plaintext column. Or — preferred — accelerate the long-standing plan to drop
the legacy table once every reader has been migrated to `user_sessions`
(the bottom-of-`db.py` comment says this is the eventual direction).

---

### MED-1 — Per-email login rate limit is per-process and weakly bounded

**Where.** `/auth/login` handler — `server_features.py:1696-1754`.

```python
if email_key and server._is_rate_limited(f"email:{email_key}:login", limit=5, window=600):
    return JSONResponse({"error": "Too many attempts..."}, status_code=429, ...)
```

`_is_rate_limited` (`server.py:1653`) uses Redis when available and falls back
to a per-process in-memory sliding window. With Redis available the limit is
correct (5 per 10 min per email globally). Without Redis the limit is
*per worker process* — a deployment running 4 uvicorn workers behind the
tunnel gives the attacker 20 wrong-guesses per 10 min per email, and even
under Redis the failure-count is not persisted across a Redis flush.

Worse: the dedicated `is_login_locked` / `record_login_failure` /
`clear_login_failures` triple in this module (`auth.py:81-117`) is a
*persistent SQLite-backed* implementation with the right semantics
(keyed on identifier+ip pair, 5 in 15min default) — **but it is not called
by `/auth/login`**. `grep` confirms zero call sites outside this file and the
db.py re-export shim. The persistent ceiling (30 failures/24h) that the
in-process `_is_account_locked` would otherwise apply *is also not wired*
into `/auth/login` — that code path lives in the dead legacy `/login` form
handler that just redirects to `/token`.

**Recommendation.** Wire `db.is_login_locked` / `db.record_login_failure` into
the `/auth/login` handler before the `verify_password` call, and
`db.clear_login_failures` on success. Persisted, survives restarts, shared
across workers. Drop the in-process `_login_failures` dict.

---

### MED-2 — Password-reset table retains raw token alongside the hash

**Where.** `auth.py:454-471` (`create_password_reset`), `auth.py:474-493`
(`get_password_reset`, `use_password_reset`), `server.py:5067-5089` (`_lookup_reset`).

```python
def create_password_reset(user_id: int) -> str:
    token = secrets.token_urlsafe(36)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    ...
    c.execute(
        "INSERT INTO password_resets (user_id, token, token_hash, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, token, token_hash, ...),
    )
```

Migration 003 added `token_hash` (good) but the original `token TEXT UNIQUE NOT NULL`
column is still required and still written with plaintext. `_lookup_reset` queries
the hash first then falls back to the plaintext column — meaning the plaintext
column is still a live authoritative credential store. `use_password_reset`
only flips `used=1`; the raw token stays in the row indefinitely until the next
`purge_expired_resets` sweep. `get_password_reset` (the still-exported
function on line 474) actually queries by *plaintext token only* — bypassing
the hash hardening entirely.

**Recommendation.** Stop writing the plaintext column in `create_password_reset`
(use `token_hash` only); update `get_password_reset` to look up by hash, not raw;
add a migration to NULL the existing plaintext column and drop it after rollover.
The whole point of migration 003 was to remove this exposure.

---

### MED-3 — `consume_backup_code` / `store_backup_codes` reference undefined `_json_2fa`

**Where.** `auth.py:576`, `auth.py:591`, `auth.py:619`.

```python
blob = _json_2fa.dumps(hashed_codes)
...
return _json_2fa.loads(row["backup_codes"]) or []
...
blob = _json_2fa.dumps(codes)
```

The name `_json_2fa` is not imported, defined, or aliased anywhere in this
file (`grep -n "_json_2fa\|json as _json_2fa" gateway/queries/auth.py gateway/db.py`
returns only these three call sites). `import json` is present at line 12 but
not aliased. First call to any of these three functions raises `NameError`.

This sits *adjacent* to the audit scope (it's the backup-code 2FA path, not
hash/salt/session/reset), but the failure mode is security-relevant: the
backup-code recovery path is the user's last resort if they lose a TOTP
device, and a `NameError` there means the user is locked out at exactly the
moment they need to recover. Worse, the comment at `/auth/login` line 1771
says "2FA was removed — login always completes without a second factor",
suggesting these functions may be exported into a non-functional 2FA stub
that other callers (admin re-prompt? backup-code consumer) may still hit.

**Recommendation.** Either delete the backup-code helpers if the 2FA path is
truly dead (the `disable_user_2fa` / `set_user_2fa_method` columns suggest
otherwise), or replace `_json_2fa` with `json`. This is a one-line fix; flagged
as MED rather than LOW only because it lives in the auth module and the
failure is silent until the function is actually called.

---

### LOW-1 — Legacy PBKDF2 iteration count still accepted (200k)

**Where.** `auth.py:25-26`, `verify_password` at `auth.py:141-146`.

```python
PBKDF2_ITERATIONS = 600_000
PBKDF2_LEGACY_ITERATIONS = 200_000
...
def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    candidate, _ = _hash_password(password, salt, PBKDF2_ITERATIONS)
    if hmac.compare_digest(candidate, stored_hash):
        return True
    legacy, _ = _hash_password(password, salt, PBKDF2_LEGACY_ITERATIONS)
    return hmac.compare_digest(legacy, stored_hash)
```

The modern count (600k) meets OWASP 2023+ for PBKDF2-SHA256. The legacy fallback
(200k) is below OWASP and offers attackers a 3× speedup if a hash dump is
exfiltrated and the targeted user has not yet logged in since the bump.

Mitigation is partial: `password_needs_rehash` exists and `/auth/login` does
call it (`server_features.py:1760-1769`) — so users self-upgrade on next login.
The exposure is the long tail of inactive accounts whose hash is still at 200k.

**Recommendation.** Add a one-shot batch job that re-hashes every legacy row by
running `_hash_password(stored_hash || pepper, salt, 600_000)` over the
already-hashed material (PBKDF2 over PBKDF2 is sound) and updates in place.
Then drop `PBKDF2_LEGACY_ITERATIONS` from `verify_password`.

---

### LOW-2 — Salt is `secrets.token_hex(16)` → 16 hex chars, ie 8 bytes of entropy stored as hex

**Where.** `auth.py:130-138`.

```python
def _hash_password(password, salt=None, iterations=PBKDF2_ITERATIONS):
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return dk.hex(), salt
```

Wait — `secrets.token_hex(16)` returns 32 hex characters representing 16 random
bytes (the argument is *number of bytes*, not characters). So the underlying
random material is 16 bytes (128 bits). PBKDF2 then sees `salt.encode()` =
**32 bytes of ASCII hex**, which is what actually gets fed into the KDF. This
exceeds the OWASP minimum of 16 bytes — the audit hard requirement is met.

The minor wart is that the salt is stored as hex rather than the raw bytes
or base64, which doubles the row size for no security gain (~20 bytes per
user on disk). Pure aesthetic, not a finding worth fixing.

**Recommendation.** No action required. Salt entropy meets the bar.

---

### LOW-3 — Login-failure persistence table is GC'd at 24h, but no upper cap

**Where.** `auth.py:112-117` (`login_failures_gc`) — only called from
ops cron; never auto-vacuumed. If `record_login_failure` IS wired into
`/auth/login` (per MED-1 above), an attacker can fill the table with rows
keyed on victim-emails to slow legitimate logins (every `is_login_locked`
call is a `COUNT(*)` scan over the matching key/IP — indexed lookup, but
unbounded growth degrades the index over time).

**Recommendation.** Cap `record_login_failure` to N rows per (identifier,ip)
key at insert time, or schedule `login_failures_gc(86400)` from the existing
purge-expired-sessions cron job.

---

### INFO-1 — `hmac.compare_digest` is used correctly everywhere it matters

`verify_password` (`auth.py:143, 146`), `password_needs_rehash` (`auth.py:156`).
No `==` comparison of hash material anywhere in this file. Audit requirement met.

---

### INFO-2 — Hardened-session entropy & hashing are correct

`create_user_session` (`auth.py:784`): `secrets.token_hex(32)` → 32 bytes (256 bits)
of entropy, encoded as 64 hex chars. Hashed via `_hash_session_token` →
`hashlib.sha256` before storage (`auth.py:765-766, 785, 808`). Validate
flows hash on lookup (`auth.py:828`). Reset-token entropy
(`auth.py:462`): `secrets.token_urlsafe(36)` → 36 bytes (288 bits). All three
meet the entropy bar comfortably.

---

## Out of scope but noted

- `delete_session(token)` on the legacy table doesn't hash — same plaintext-storage
  issue as HIGH-1.
- `_ensure_invite_expires_at_column` runs an unconditional `ALTER TABLE` at
  import time inside a `try/except OperationalError` — fine on SQLite, but it
  means every process boot acquires a write lock. Not exploitable.
- `cascade_delete_user` (`auth.py:870-902`) does an `f"DELETE FROM {table} WHERE user_id = ?"`
  with the `table` name interpolated. Source is `sqlite_master`, so it's not
  user-controllable, but if a malicious admin can `CREATE TABLE` (they cannot),
  this could be a vector. Information only.
