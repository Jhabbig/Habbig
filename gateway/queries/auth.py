"""Queries extracted from gateway/db.py — auth domain.

Moved out of db.py to keep the connection-pooling/schema module small.
Re-exported back onto db.py at import time, so every existing
``import db; db.<name>`` call site keeps working unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from typing import Optional

import db

# PBKDF2 iteration counts.
# Modern accounts: 600k — OWASP 2023+ recommendation for SHA-256.
# Legacy: 200k — older rows still verify; callers trigger a rehash
# via ``password_needs_rehash`` on next successful login.
PBKDF2_ITERATIONS = 600_000
PBKDF2_LEGACY_ITERATIONS = 200_000


SESSION_TTL = 90 * 24 * 60 * 60  # 90 days (3 months)


INVITE_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


RESET_TTL = 60 * 60  # 1 hour


SESSION_HARDENED_TTL = 7 * 24 * 60 * 60  # 7 days


MAX_SESSIONS_PER_USER = 3


def rate_limit_hit(key: str, limit: int, window: int) -> bool:
    """Record a hit and return True if this hit exceeds *limit* within *window* seconds.

    Atomic: GC of old rows, count, insert, all in a single transaction.
    Callers should treat True as "deny this request".
    """
    now = time.time()
    cutoff = now - window
    with db.conn() as c:
        c.execute("DELETE FROM rate_limits WHERE key = ? AND ts < ?", (key, cutoff))
        row = c.execute("SELECT COUNT(*) AS n FROM rate_limits WHERE key = ? AND ts >= ?", (key, cutoff)).fetchone()
        count = int(row["n"] if row else 0)
        if count >= limit:
            return True
        c.execute("INSERT INTO rate_limits (key, ts) VALUES (?, ?)", (key, now))
    return False


def rate_limit_check(key: str, limit: int, window: int) -> bool:
    """Non-destructive check: return True if *key* has hit *limit* within *window* seconds.
    Does NOT record a new hit. Use for dry-run checks (e.g. middleware early-out).
    """
    now = time.time()
    cutoff = now - window
    with db.conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM rate_limits WHERE key = ? AND ts >= ?", (key, cutoff)).fetchone()
        return int(row["n"] if row else 0) >= limit


def rate_limit_gc(max_age_seconds: int = 86400) -> int:
    """Garbage-collect rate_limits rows older than max_age_seconds. Returns rows deleted."""
    cutoff = time.time() - max_age_seconds
    with db.conn() as c:
        cur = c.execute("DELETE FROM rate_limits WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0


def record_login_failure(identifier: str, ip: str) -> None:
    """Log a failed login attempt keyed on (identifier, ip)."""
    with db.conn() as c:
        c.execute("INSERT INTO login_failures (identifier, ip, ts) VALUES (?, ?, ?)",
                  (identifier.lower(), ip or "unknown", time.time()))


def is_login_locked(identifier: str, ip: str, threshold: int = 5, window: int = 900) -> bool:
    """True if (identifier, ip) pair has >= threshold failures within window seconds.

    Keying on the pair (rather than identifier alone) prevents a remote attacker
    from locking out the victim by spamming failed attempts from another IP.
    """
    cutoff = time.time() - window
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM login_failures WHERE identifier = ? AND ip = ? AND ts >= ?",
            (identifier.lower(), ip or "unknown", cutoff),
        ).fetchone()
        return int(row["n"] if row else 0) >= threshold


def clear_login_failures(identifier: str, ip: str = "") -> None:
    """Clear login failures for identifier. If ip provided, only clear for that ip."""
    with db.conn() as c:
        if ip:
            c.execute("DELETE FROM login_failures WHERE identifier = ? AND ip = ?", (identifier.lower(), ip))
        else:
            c.execute("DELETE FROM login_failures WHERE identifier = ?", (identifier.lower(),))


def login_failures_gc(max_age_seconds: int = 86400) -> int:
    """Garbage-collect login_failures rows older than max_age_seconds."""
    cutoff = time.time() - max_age_seconds
    with db.conn() as c:
        cur = c.execute("DELETE FROM login_failures WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0


def delete_sessions_for_user(user_id: int, except_token: str = "") -> int:
    """Delete all sessions for user_id, optionally preserving one by token. Returns rows deleted."""
    with db.conn() as c:
        if except_token:
            cur = c.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?", (user_id, except_token))
        else:
            cur = c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return cur.rowcount or 0


def _hash_password(
    password: str,
    salt: Optional[str] = None,
    iterations: int = PBKDF2_ITERATIONS,
) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return dk.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    candidate, _ = _hash_password(password, salt, PBKDF2_ITERATIONS)
    if hmac.compare_digest(candidate, stored_hash):
        return True
    legacy, _ = _hash_password(password, salt, PBKDF2_LEGACY_ITERATIONS)
    return hmac.compare_digest(legacy, stored_hash)


def password_needs_rehash(password: str, stored_hash: str, salt: str) -> bool:
    """True when the verified hash was computed at the legacy iteration count.

    Callers should re-hash + UPDATE on the next successful login so users
    opportunistically migrate to the modern PBKDF2 iteration count.
    """
    modern, _ = _hash_password(password, salt, PBKDF2_ITERATIONS)
    return not hmac.compare_digest(modern, stored_hash)


def create_user(email: str, password: str, username: str = "", is_admin: bool = False, admin_level: int = 0) -> int:
    email = email.lower().strip()
    username = username.strip()
    if not username:
        username = email.split("@")[0]
    level = admin_level if admin_level else (1 if is_admin else 0)
    pwd_hash, salt = _hash_password(password)
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, password_hash, password_salt, created_at, is_admin) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (username, email, pwd_hash, salt, int(time.time()), level),
        )
        return cur.lastrowid


def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
    return row


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip(),)
        ).fetchone()


def get_user_by_email_or_username(identifier: str) -> Optional[sqlite3.Row]:
    """Look up a user by email or username."""
    identifier = identifier.strip()
    if "@" in identifier:
        return get_user_by_email(identifier)
    return get_user_by_username(identifier)


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def set_default_dashboard(user_id: int, dashboard_key: Optional[str]) -> None:
    """Store the user's preferred landing dashboard (or clear it with None)."""
    with db.conn() as c:
        c.execute(
            "UPDATE users SET default_dashboard = ? WHERE id = ?",
            (dashboard_key, user_id),
        )


def get_default_dashboard(user_id: int) -> Optional[str]:
    with db.conn() as c:
        row = c.execute(
            "SELECT default_dashboard FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return row["default_dashboard"] if row else None


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(48)
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + SESSION_TTL),
        )
    return token


def get_session(token: str) -> Optional[sqlite3.Row]:
    if not token:
        return None
    with db.conn() as c:
        row = c.execute(
            "SELECT s.*, u.username, u.email, u.is_admin FROM sessions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > ?",
            (token, int(time.time())),
        ).fetchone()
    return row


def delete_session(token: str) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions() -> int:
    with db.conn() as c:
        cur = c.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
        return cur.rowcount


def set_session_csrf(session_token: str, csrf_token: str) -> None:
    """Store a CSRF token in the session row."""
    with db.conn() as c:
        c.execute(
            "UPDATE sessions SET csrf_token = ?, csrf_created_at = ? WHERE token = ?",
            (csrf_token, int(time.time()), session_token),
        )


def get_session_csrf(session_token: str) -> Optional[dict]:
    """Get the CSRF token and creation time for a session."""
    if not session_token:
        return None
    with db.conn() as c:
        row = c.execute(
            "SELECT csrf_token, csrf_created_at FROM sessions WHERE token = ? AND expires_at > ?",
            (session_token, int(time.time())),
        ).fetchone()
    if not row or not row["csrf_token"]:
        return None
    return {"csrf_token": row["csrf_token"], "csrf_created_at": row["csrf_created_at"]}


def clear_session_csrf(session_token: str) -> None:
    """Clear the CSRF token from a session (e.g. on logout)."""
    with db.conn() as c:
        c.execute(
            "UPDATE sessions SET csrf_token = NULL, csrf_created_at = NULL WHERE token = ?",
            (session_token,),
        )


def _ensure_invite_expires_at_column() -> None:
    """Idempotent ALTER TABLE to add expires_at column for existing DBs."""
    try:
        with db.conn() as c:
            c.execute("ALTER TABLE invite_tokens ADD COLUMN expires_at INTEGER")
    except sqlite3.OperationalError:
        pass  # Column already exists.


def generate_invite_token() -> str:
    """Generate a 32-character URL-safe random invite token."""
    return secrets.token_urlsafe(24)


def create_invite_token(note: str = "", target_email: str = "") -> str:
    """Create a new unclaimed invite token. Returns the token string."""
    token = generate_invite_token()
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO invite_tokens (token, status, note, target_email, created_at, expires_at) "
            "VALUES (?, 'unclaimed', ?, ?, ?, ?)",
            (token, note, target_email.strip() or None, now, now + INVITE_TOKEN_TTL_SECONDS),
        )
    return token


def get_invite_token(token: str) -> Optional[sqlite3.Row]:
    token = token.strip()
    now = int(time.time())
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM invite_tokens WHERE token = ? "
            "AND status = 'unclaimed' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (token, now),
        ).fetchone()


def claim_invite_token(token_str: str, user_id: int, email: str) -> bool:
    """Atomically claim a token. Returns True if claimed, False if already claimed or expired."""
    token_str = token_str.strip()
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "UPDATE invite_tokens SET status = 'claimed', claimed_by_user_id = ?, "
            "claimed_by_email = ?, claimed_at = ? "
            "WHERE token = ? AND status = 'unclaimed' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (user_id, email, now, token_str, now),
        )
        if cur.rowcount == 0:
            return False  # Already claimed, revoked, or expired
        c.execute("UPDATE users SET invite_token_id = (SELECT id FROM invite_tokens WHERE token = ?) WHERE id = ?",
                   (token_str, user_id))
        return True


def revoke_invite_token(token_id: int) -> None:
    with db.conn() as c:
        c.execute("UPDATE invite_tokens SET status = 'revoked' WHERE id = ? AND status = 'unclaimed'", (token_id,))


def list_invite_tokens() -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute("SELECT * FROM invite_tokens ORDER BY created_at DESC").fetchall()


def list_all_users() -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()


def set_user_role(user_id: int, level: int) -> None:
    """Set user role: 0=user, 1=admin, 2=super_admin.

    Revokes all hardened sessions for the user after a role change so the
    new privilege level cannot be exercised on a pre-existing cookie.
    """
    with db.conn() as c:
        c.execute("UPDATE users SET is_admin = ? WHERE id = ?", (level, user_id))
    try:
        revoke_all_user_sessions(user_id)
    except Exception:
        pass  # Revocation best-effort; DB row change already committed.


def set_user_admin(user_id: int, is_admin: bool) -> None:
    """Legacy helper — promotes to admin (1) or demotes to user (0)."""
    set_user_role(user_id, 1 if is_admin else 0)


def set_user_suspended(user_id: int, suspended: bool) -> None:
    with db.conn() as c:
        c.execute("UPDATE users SET suspended = ? WHERE id = ?", (1 if suspended else 0, user_id))
        if suspended:
            # Kill all sessions for this user
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def mask_email(email: str) -> str:
    """Mask email like sh***@gmail.com."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.rsplit("@", 1)
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[:2]}***@{domain}"


def create_password_reset(user_id: int) -> str:
    """Create a password reset token (expires in 1 hour). Returns the raw token.

    Stores BOTH the raw `token` (for backwards compatibility with any legacy
    reset link that's still in the wild) AND `token_hash` (Feature 2: at-rest
    hardening — lookups prefer the hash column). When the legacy column is
    eventually removed the migration just drops it.
    """
    token = secrets.token_urlsafe(36)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO password_resets (user_id, token, token_hash, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, token, token_hash, now, now + RESET_TTL),
        )
    return token


def get_password_reset(token: str) -> Optional[sqlite3.Row]:
    """Get a valid (not expired, not used) password reset record."""
    if not token:
        return None
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM password_resets "
            "WHERE token = ? AND used = 0 AND expires_at > ?",
            (token, int(time.time())),
        ).fetchone()


def use_password_reset(token: str) -> bool:
    """Atomically mark a reset token as used. Returns True if successful."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE password_resets SET used = 1 WHERE token = ? AND used = 0",
            (token,),
        )
        return cur.rowcount > 0


def purge_expired_resets() -> int:
    """Delete expired or used reset tokens."""
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM password_resets WHERE expires_at <= ? OR used = 1",
            (int(time.time()),),
        )
        return cur.rowcount


def get_user_2fa_status(user_id: int) -> Optional[sqlite3.Row]:
    """Return the 2FA-relevant columns from users, or None if user not found."""
    with db.conn() as c:
        return c.execute(
            "SELECT id, email, username, is_admin, totp_enabled, totp_secret, "
            "totp_setup_at, email_otp_enabled, two_fa_method, two_fa_verified_at, "
            "backup_codes, backup_codes_generated_at "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


def set_user_2fa_method(
    user_id: int,
    method: Optional[str],
    encrypted_secret: Optional[str] = None,
) -> None:
    """Enable 2FA with *method* ("totp"|"email_otp"|None to disable).

    When method is "totp", *encrypted_secret* must be the Fernet-encrypted base32.
    Flips the matching `*_enabled` column and sets `totp_setup_at` for TOTP.
    """
    now = int(time.time())
    with db.conn() as c:
        if method == "totp":
            c.execute(
                "UPDATE users SET two_fa_method = ?, totp_enabled = 1, totp_secret = ?, "
                "totp_setup_at = ?, email_otp_enabled = 0 WHERE id = ?",
                (method, encrypted_secret, now, user_id),
            )
        elif method == "email_otp":
            c.execute(
                "UPDATE users SET two_fa_method = ?, email_otp_enabled = 1, "
                "totp_enabled = 0, totp_secret = NULL, totp_setup_at = NULL WHERE id = ?",
                (method, user_id),
            )
        else:
            # method=None → disable (use disable_user_2fa for a clean wipe)
            c.execute(
                "UPDATE users SET two_fa_method = NULL, totp_enabled = 0, "
                "totp_secret = NULL, totp_setup_at = NULL, email_otp_enabled = 0 "
                "WHERE id = ?",
                (user_id,),
            )


def disable_user_2fa(user_id: int) -> None:
    """Clear all 2FA state for a user — method, secrets, backup codes."""
    with db.conn() as c:
        c.execute(
            "UPDATE users SET two_fa_method = NULL, totp_enabled = 0, "
            "totp_secret = NULL, totp_setup_at = NULL, email_otp_enabled = 0, "
            "backup_codes = NULL, backup_codes_generated_at = NULL WHERE id = ?",
            (user_id,),
        )
        # Also clear any fresh-verification state on sessions for this user,
        # so subsequent admin pages re-prompt for 2FA.
        c.execute(
            "UPDATE sessions SET two_fa_verified = 0, two_fa_verified_at = NULL WHERE user_id = ?",
            (user_id,),
        )


def store_backup_codes(user_id: int, hashed_codes: list[dict]) -> None:
    """Persist backup codes as a JSON array of {hash, salt, used_at} dicts.

    Caller generates plaintext codes, hashes each, then calls this exactly once.
    The plaintext is shown to the user only at that moment.
    """
    now = int(time.time())
    blob = _json_2fa.dumps(hashed_codes)
    with db.conn() as c:
        c.execute(
            "UPDATE users SET backup_codes = ?, backup_codes_generated_at = ? WHERE id = ?",
            (blob, now, user_id),
        )


def get_backup_codes(user_id: int) -> list[dict]:
    """Return the raw hashed backup code list (or empty list if unset)."""
    with db.conn() as c:
        row = c.execute("SELECT backup_codes FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row["backup_codes"]:
        return []
    try:
        return _json_2fa.loads(row["backup_codes"]) or []
    except (ValueError, TypeError):
        return []


def consume_backup_code(user_id: int, plaintext_code: str) -> bool:
    """Try to match *plaintext_code* against one unused backup code.

    On success, marks that entry's `used_at` and returns True. Constant-time
    comparison, single-pass over the JSON array.
    """
    codes = get_backup_codes(user_id)
    if not codes:
        return False
    matched = False
    for entry in codes:
        if entry.get("used_at"):
            continue
        stored_hash = entry.get("hash", "")
        salt = entry.get("salt", "")
        if not stored_hash or not salt:
            continue
        if verify_password(plaintext_code, stored_hash, salt):
            entry["used_at"] = int(time.time())
            matched = True
            break
    if not matched:
        return False
    blob = _json_2fa.dumps(codes)
    with db.conn() as c:
        c.execute("UPDATE users SET backup_codes = ? WHERE id = ?", (blob, user_id))
    return True


def count_remaining_backup_codes(user_id: int) -> int:
    codes = get_backup_codes(user_id)
    return sum(1 for c in codes if not c.get("used_at"))


def insert_2fa_attempt(user_id: int, method: str, success: bool, ip: str) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO two_fa_attempts (user_id, method, success, ip_address, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, method, 1 if success else 0, ip or "unknown", int(time.time())),
        )


def recent_2fa_failures(user_id: int, ip: str, window_seconds: int = 600) -> int:
    cutoff = int(time.time()) - window_seconds
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM two_fa_attempts "
            "WHERE user_id = ? AND ip_address = ? AND success = 0 AND created_at >= ?",
            (user_id, ip or "unknown", cutoff),
        ).fetchone()
    return int(row["n"] if row else 0)


def insert_email_otp(
    user_id: int,
    code_hash: str,
    code_salt: str,
    ip: str = "",
    ttl_seconds: int = 600,
) -> int:
    now = int(time.time())
    # Supersede any prior unused OTP for this user so only one is ever active.
    with db.conn() as c:
        c.execute(
            "UPDATE email_otps SET used_at = ? "
            "WHERE user_id = ? AND used_at IS NULL",
            (now, user_id),
        )
        cur = c.execute(
            "INSERT INTO email_otps (user_id, code_hash, code_salt, created_at, expires_at, ip_address) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, code_hash, code_salt, now, now + ttl_seconds, ip or "unknown"),
        )
        return cur.lastrowid


def get_active_email_otp(user_id: int) -> Optional[sqlite3.Row]:
    now = int(time.time())
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM email_otps WHERE user_id = ? AND used_at IS NULL "
            "AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
            (user_id, now),
        ).fetchone()


def mark_email_otp_used(otp_id: int) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE email_otps SET used_at = ? WHERE id = ?",
            (int(time.time()), otp_id),
        )


def purge_expired_email_otps() -> int:
    cutoff = int(time.time()) - 3600  # keep 1h for debugging, then drop
    with db.conn() as c:
        cur = c.execute("DELETE FROM email_otps WHERE expires_at < ?", (cutoff,))
        return cur.rowcount or 0


def mark_session_two_fa_verified(session_token: str) -> None:
    """Flip sessions.two_fa_verified=1 for the given token and stamp the time.
    Also stamps users.two_fa_verified_at for the "last used" indicator."""
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE sessions SET two_fa_verified = 1, two_fa_verified_at = ? WHERE token = ?",
            (now, session_token),
        )
        c.execute(
            "UPDATE users SET two_fa_verified_at = ? "
            "WHERE id = (SELECT user_id FROM sessions WHERE token = ?)",
            (now, session_token),
        )


def session_two_fa_verified(session_token: str) -> bool:
    if not session_token:
        return False
    with db.conn() as c:
        row = c.execute(
            "SELECT two_fa_verified FROM sessions WHERE token = ? AND expires_at > ?",
            (session_token, int(time.time())),
        ).fetchone()
    return bool(row and row["two_fa_verified"])


def set_pending_totp_secret(session_token: str, encrypted_secret: str) -> None:
    """Stash a pending Fernet-encrypted TOTP secret on the session row.

    Used between GET /api/auth/2fa/totp/setup and POST verify-setup so the
    candidate secret survives the round-trip without hitting a new table.
    Cleared on verify-setup.
    """
    with db.conn() as c:
        c.execute(
            "UPDATE sessions SET pending_totp_secret = ?, pending_totp_secret_at = ? "
            "WHERE token = ?",
            (encrypted_secret, int(time.time()), session_token),
        )


def get_pending_totp_secret(session_token: str, max_age_seconds: int = 900) -> Optional[str]:
    """Return the pending encrypted TOTP secret if set and still fresh (<15min)."""
    if not session_token:
        return None
    with db.conn() as c:
        row = c.execute(
            "SELECT pending_totp_secret, pending_totp_secret_at FROM sessions WHERE token = ?",
            (session_token,),
        ).fetchone()
    if not row or not row["pending_totp_secret"]:
        return None
    if int(time.time()) - int(row["pending_totp_secret_at"] or 0) > max_age_seconds:
        return None
    return row["pending_totp_secret"]


def clear_pending_totp_secret(session_token: str) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE sessions SET pending_totp_secret = NULL, pending_totp_secret_at = NULL "
            "WHERE token = ?",
            (session_token,),
        )


def _hash_session_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_user_session(
    user_id: int,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    legacy_token: Optional[str] = None,
    ttl_seconds: int = SESSION_HARDENED_TTL,
) -> str:
    """Issue a new hardened session. Returns the raw token (store in cookie).

    Enforces MAX_SESSIONS_PER_USER by revoking the oldest active session
    before inserting the new one. If `legacy_token` is provided, it's
    recorded alongside so the legacy `sessions` table lookup (CSRF etc)
    keeps working for this session.
    """
    raw = secrets.token_hex(32)  # 64 hex chars
    token_hash = _hash_session_token(raw)
    now = int(time.time())
    with db.conn() as c:
        active = c.execute(
            "SELECT id FROM user_sessions "
            "WHERE user_id = ? AND revoked = 0 AND expires_at > ? "
            "ORDER BY last_active_at ASC",
            (user_id, now),
        ).fetchall()
        if len(active) >= MAX_SESSIONS_PER_USER:
            to_revoke = len(active) - MAX_SESSIONS_PER_USER + 1
            oldest_ids = [r["id"] for r in active[:to_revoke]]
            c.executemany(
                "UPDATE user_sessions SET revoked = 1, revoked_at = ? WHERE id = ?",
                [(now, sid) for sid in oldest_ids],
            )
        c.execute(
            "INSERT INTO user_sessions "
            "(user_id, token_hash, legacy_token, created_at, expires_at, "
            "last_active_at, ip_address, user_agent, revoked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                user_id,
                token_hash,
                legacy_token,
                now,
                now + ttl_seconds,
                now,
                (ip_address or "")[:64],
                (user_agent or "")[:256],
            ),
        )
    return raw


def validate_user_session(raw_token: str) -> Optional[sqlite3.Row]:
    """Look up a hardened session by raw cookie value.

    Hashes the raw token, finds the row, and updates last_active_at.
    Returns None for unknown / revoked / expired sessions.
    """
    if not raw_token:
        return None
    token_hash = _hash_session_token(raw_token)
    now = int(time.time())
    with db.conn() as c:
        row = c.execute(
            "SELECT us.*, u.username, u.email, u.is_admin "
            "FROM user_sessions us "
            "JOIN users u ON u.id = us.user_id "
            "WHERE us.token_hash = ? AND us.revoked = 0 AND us.expires_at > ?",
            (token_hash, now),
        ).fetchone()
        if row:
            c.execute(
                "UPDATE user_sessions SET last_active_at = ? WHERE id = ?",
                (now, row["id"]),
            )
    return row


def list_user_sessions(user_id: int) -> list[sqlite3.Row]:
    """Active sessions for a user, most-recently-active first."""
    now = int(time.time())
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM user_sessions "
            "WHERE user_id = ? AND revoked = 0 AND expires_at > ? "
            "ORDER BY last_active_at DESC",
            (user_id, now),
        ).fetchall()


def revoke_user_session(session_id: int, user_id: int) -> bool:
    """Revoke a single session by id. Returns False if not owned."""
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "UPDATE user_sessions SET revoked = 1, revoked_at = ? "
            "WHERE id = ? AND user_id = ? AND revoked = 0",
            (now, session_id, user_id),
        )
        return cur.rowcount > 0


def cascade_delete_user(user_id: int) -> dict:
    """Delete a user and every row in any table that has a `user_id` column.

    Used by the user-initiated account-deletion flow (GDPR Art. 17) and the
    admin delete flow. Returns a dict mapping table names to deleted-row
    counts so callers can audit the scope. Fails open: a table that's missing
    or schema-mismatched is skipped rather than aborting the whole delete.
    """
    deleted: dict = {}
    with db.conn() as c:
        # Enumerate every user-scoped table by inspecting the schema.
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for r in rows:
            table = r["name"]
            if table == "users":
                continue  # Delete users last so FK-ish cascades don't orphan.
            try:
                cols = [c2["name"] for c2 in c.execute(f"PRAGMA table_info({table})").fetchall()]
            except Exception:
                continue
            if "user_id" in cols:
                try:
                    cur = c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                    if cur.rowcount:
                        deleted[table] = cur.rowcount
                except Exception:
                    continue
        # Then the user row itself.
        cur = c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        deleted["users"] = cur.rowcount
    return deleted


def revoke_user_session_by_token(raw_token: str) -> bool:
    """Revoke a session by its raw cookie value. Used by POST /auth/logout."""
    if not raw_token:
        return False
    token_hash = _hash_session_token(raw_token)
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "UPDATE user_sessions SET revoked = 1, revoked_at = ? WHERE token_hash = ? AND revoked = 0",
            (now, token_hash),
        )
        return cur.rowcount > 0


def revoke_all_other_user_sessions(user_id: int, current_token_hash: str) -> int:
    """Revoke every active session for this user except the current one.

    Used by "Sign out all other sessions" in settings. Returns count revoked.
    """
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "UPDATE user_sessions SET revoked = 1, revoked_at = ? "
            "WHERE user_id = ? AND revoked = 0 AND token_hash != ?",
            (now, user_id, current_token_hash),
        )
        return cur.rowcount


def revoke_all_user_sessions(user_id: int) -> int:
    """Kill every active session for a user (used on password reset)."""
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "UPDATE user_sessions SET revoked = 1, revoked_at = ? "
            "WHERE user_id = ? AND revoked = 0",
            (now, user_id),
        )
        return cur.rowcount


def rotate_session(
    old_raw_token: str,
    user_id: int,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Optional[str]:
    """Revoke the current session and issue a fresh one for the same user.

    Spec STEP 9: "After any role change (promotion to admin, 2FA completion),
    revoke old session and issue a new session token." Callers should swap
    the cookie on the response object after calling this.

    Returns the new raw token, or None if the old token could not be
    validated (e.g. already revoked, wrong user, expired). Never raises.
    """
    if not old_raw_token:
        return None
    old = validate_user_session(old_raw_token)
    if not old or old["user_id"] != user_id:
        return None
    # Revoke first so a crash between the two calls can never leave both
    # tokens alive for the same privilege-change transition.
    revoke_user_session_by_token(old_raw_token)
    return create_user_session(
        user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )


# Bootstrap: ensure invite_tokens has the expires_at column. Ran from
# db.py at import time before; now fires when this module is imported,
# which happens at the bottom of db.py via the re-export block.
_ensure_invite_expires_at_column()


__all__ = [
    'PBKDF2_ITERATIONS',
    'PBKDF2_LEGACY_ITERATIONS',
    'SESSION_TTL',
    'INVITE_TOKEN_TTL_SECONDS',
    'RESET_TTL',
    'SESSION_HARDENED_TTL',
    'MAX_SESSIONS_PER_USER',
    'rate_limit_hit',
    'rate_limit_check',
    'rate_limit_gc',
    'record_login_failure',
    'is_login_locked',
    'clear_login_failures',
    'login_failures_gc',
    'delete_sessions_for_user',
    'verify_password',
    'password_needs_rehash',
    'create_user',
    'get_user_by_email',
    'get_user_by_username',
    'get_user_by_email_or_username',
    'get_user_by_id',
    'set_default_dashboard',
    'get_default_dashboard',
    'create_session',
    'get_session',
    'delete_session',
    'purge_expired_sessions',
    'set_session_csrf',
    'get_session_csrf',
    'clear_session_csrf',
    'generate_invite_token',
    'create_invite_token',
    'get_invite_token',
    'claim_invite_token',
    'revoke_invite_token',
    'list_invite_tokens',
    'list_all_users',
    'set_user_role',
    'set_user_admin',
    'set_user_suspended',
    'mask_email',
    'create_password_reset',
    'get_password_reset',
    'use_password_reset',
    'purge_expired_resets',
    'get_user_2fa_status',
    'set_user_2fa_method',
    'disable_user_2fa',
    'store_backup_codes',
    'get_backup_codes',
    'consume_backup_code',
    'count_remaining_backup_codes',
    'insert_2fa_attempt',
    'recent_2fa_failures',
    'insert_email_otp',
    'get_active_email_otp',
    'mark_email_otp_used',
    'purge_expired_email_otps',
    'mark_session_two_fa_verified',
    'session_two_fa_verified',
    'set_pending_totp_secret',
    'get_pending_totp_secret',
    'clear_pending_totp_secret',
    'create_user_session',
    'validate_user_session',
    'list_user_sessions',
    'revoke_user_session',
    'cascade_delete_user',
    'revoke_user_session_by_token',
    'revoke_all_other_user_sessions',
    'revoke_all_user_sessions',
    'rotate_session',
]
