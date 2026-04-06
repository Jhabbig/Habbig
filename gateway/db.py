"""SQLite layer for the gateway — users, sessions, subscriptions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "auth.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    username          TEXT UNIQUE NOT NULL,
    email             TEXT UNIQUE NOT NULL,
    password_hash     TEXT NOT NULL,
    password_salt     TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    is_admin          INTEGER NOT NULL DEFAULT 0,
    suspended         INTEGER NOT NULL DEFAULT 0,
    default_dashboard TEXT,
    invite_token_id   INTEGER REFERENCES invite_tokens(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    dashboard_key   TEXT NOT NULL,
    plan            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    started_at      INTEGER NOT NULL,
    expires_at      INTEGER,
    stripe_sub_id   TEXT,
    source          TEXT NOT NULL DEFAULT 'placeholder',
    UNIQUE(user_id, dashboard_key),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS invite_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT UNIQUE NOT NULL,
    status          TEXT NOT NULL DEFAULT 'unclaimed',
    claimed_by_user_id INTEGER REFERENCES users(id),
    claimed_by_email TEXT,
    note            TEXT DEFAULT '',
    created_at      INTEGER NOT NULL,
    claimed_at      INTEGER
);

CREATE TABLE IF NOT EXISTS enquiries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL,
    job_title       TEXT NOT NULL,
    message         TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    read            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS password_resets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    token       TEXT UNIQUE NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_invite_token ON invite_tokens(token);
CREATE INDEX IF NOT EXISTS idx_invite_status ON invite_tokens(status);
CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        # Lightweight migrations: add columns that were introduced after the
        # original schema shipped. SQLite doesn't support IF NOT EXISTS on
        # ALTER TABLE, so we probe PRAGMA table_info and only add when missing.
        existing_cols = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
        if "default_dashboard" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN default_dashboard TEXT")
        if "suspended" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN suspended INTEGER NOT NULL DEFAULT 0")
        if "invite_token_id" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN invite_token_id INTEGER REFERENCES invite_tokens(id)")
        # invite_tokens migrations
        invite_cols = {row["name"] for row in c.execute("PRAGMA table_info(invite_tokens)")}
        if "target_email" not in invite_cols:
            c.execute("ALTER TABLE invite_tokens ADD COLUMN target_email TEXT")
        if "username" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN username TEXT")
            # Backfill: set username to email local part for existing users
            for row in c.execute("SELECT id, email FROM users WHERE username IS NULL").fetchall():
                uname = row[1].split("@")[0] if row[1] else f"user{row[0]}"
                c.execute("UPDATE users SET username = ? WHERE id = ?", (uname, row[0]))


# ── Password hashing ──────────────────────────────────────────────────────────
# Using PBKDF2-HMAC-SHA256 (stdlib, no external deps). 200k iterations.


def _hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return dk.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    candidate, _ = _hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


# ── User operations ───────────────────────────────────────────────────────────


def create_user(email: str, password: str, username: str = "", is_admin: bool = False, admin_level: int = 0) -> int:
    email = email.lower().strip()
    username = username.strip()
    if not username:
        username = email.split("@")[0]
    level = admin_level if admin_level else (1 if is_admin else 0)
    pwd_hash, salt = _hash_password(password)
    with conn() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, password_hash, password_salt, created_at, is_admin) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (username, email, pwd_hash, salt, int(time.time()), level),
        )
        return cur.lastrowid


def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
    return row


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    with conn() as c:
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
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def set_default_dashboard(user_id: int, dashboard_key: Optional[str]) -> None:
    """Store the user's preferred landing dashboard (or clear it with None)."""
    with conn() as c:
        c.execute(
            "UPDATE users SET default_dashboard = ? WHERE id = ?",
            (dashboard_key, user_id),
        )


def get_default_dashboard(user_id: int) -> Optional[str]:
    with conn() as c:
        row = c.execute(
            "SELECT default_dashboard FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return row["default_dashboard"] if row else None


# ── Session operations ────────────────────────────────────────────────────────

SESSION_TTL = 30 * 24 * 60 * 60  # 30 days


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(48)
    now = int(time.time())
    with conn() as c:
        c.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + SESSION_TTL),
        )
    return token


def get_session(token: str) -> Optional[sqlite3.Row]:
    if not token:
        return None
    with conn() as c:
        row = c.execute(
            "SELECT s.*, u.username, u.email, u.is_admin FROM sessions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > ?",
            (token, int(time.time())),
        ).fetchone()
    return row


def delete_session(token: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions() -> int:
    with conn() as c:
        cur = c.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
        return cur.rowcount


# ── Subscription operations ───────────────────────────────────────────────────


def list_subscriptions(user_id: int) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)
        ).fetchall()


def has_active_subscription(user_id: int, dashboard_key: str) -> bool:
    now = int(time.time())
    with conn() as c:
        # Admins bypass subscription checks for all dashboards.
        admin_row = c.execute(
            "SELECT is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if admin_row and admin_row[0]:
            return True
        row = c.execute(
            "SELECT id FROM subscriptions "
            "WHERE user_id = ? AND dashboard_key = ? AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (user_id, dashboard_key, now),
        ).fetchone()
    return row is not None


def upsert_subscription(
    user_id: int,
    dashboard_key: str,
    plan: str,
    duration_days: Optional[int] = None,
    source: str = "placeholder",
    stripe_sub_id: Optional[str] = None,
) -> None:
    now = int(time.time())
    expires_at = now + duration_days * 86400 if duration_days else None
    with conn() as c:
        c.execute(
            """
            INSERT INTO subscriptions
                (user_id, dashboard_key, plan, status, started_at, expires_at, stripe_sub_id, source)
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
            ON CONFLICT(user_id, dashboard_key) DO UPDATE SET
                plan        = excluded.plan,
                status      = 'active',
                started_at  = excluded.started_at,
                expires_at  = excluded.expires_at,
                stripe_sub_id = excluded.stripe_sub_id,
                source      = excluded.source
            """,
            (user_id, dashboard_key, plan, now, expires_at, stripe_sub_id, source),
        )


def cancel_subscription(user_id: int, dashboard_key: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE subscriptions SET status = 'cancelled' "
            "WHERE user_id = ? AND dashboard_key = ?",
            (user_id, dashboard_key),
        )


# ── Invite token operations ──────────────────────────────────────────────────


def generate_invite_token() -> str:
    """Generate a 32-character URL-safe random invite token."""
    return secrets.token_urlsafe(24)


def create_invite_token(note: str = "", target_email: str = "") -> str:
    """Create a new unclaimed invite token. Returns the token string."""
    token = generate_invite_token()
    with conn() as c:
        c.execute(
            "INSERT INTO invite_tokens (token, status, note, target_email, created_at) VALUES (?, 'unclaimed', ?, ?, ?)",
            (token, note, target_email.strip() or None, int(time.time())),
        )
    return token


def get_invite_token(token: str) -> Optional[sqlite3.Row]:
    token = token.strip()
    with conn() as c:
        return c.execute("SELECT * FROM invite_tokens WHERE token = ?", (token,)).fetchone()


def claim_invite_token(token_str: str, user_id: int, email: str) -> None:
    token_str = token_str.strip()
    with conn() as c:
        c.execute(
            "UPDATE invite_tokens SET status = 'claimed', claimed_by_user_id = ?, "
            "claimed_by_email = ?, claimed_at = ? WHERE token = ?",
            (user_id, email, int(time.time()), token_str),
        )
        c.execute("UPDATE users SET invite_token_id = (SELECT id FROM invite_tokens WHERE token = ?) WHERE id = ?",
                   (token_str, user_id))


def revoke_invite_token(token_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE invite_tokens SET status = 'revoked' WHERE id = ? AND status = 'unclaimed'", (token_id,))


def list_invite_tokens() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM invite_tokens ORDER BY created_at DESC").fetchall()


# ── User management (admin) ─────────────────────────────────────────────────


def list_all_users() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()


def set_user_role(user_id: int, level: int) -> None:
    """Set user role: 0=user, 1=admin, 2=super_admin."""
    with conn() as c:
        c.execute("UPDATE users SET is_admin = ? WHERE id = ?", (level, user_id))


def set_user_admin(user_id: int, is_admin: bool) -> None:
    """Legacy helper — promotes to admin (1) or demotes to user (0)."""
    set_user_role(user_id, 1 if is_admin else 0)


def set_user_suspended(user_id: int, suspended: bool) -> None:
    with conn() as c:
        c.execute("UPDATE users SET suspended = ? WHERE id = ?", (1 if suspended else 0, user_id))
        if suspended:
            # Kill all sessions for this user
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def list_all_subscriptions() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT s.*, u.email, u.username FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "ORDER BY s.started_at DESC"
        ).fetchall()


def get_revenue_stats() -> dict:
    """Return subscription counts and breakdown by dashboard and plan."""
    now = int(time.time())
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        active = c.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?)", (now,)
        ).fetchone()[0]
        cancelled = c.execute("SELECT COUNT(*) FROM subscriptions WHERE status = 'cancelled'").fetchone()[0]
        expired = c.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'active' "
            "AND expires_at IS NOT NULL AND expires_at <= ?", (now,)
        ).fetchone()[0]
        # Per-dashboard active counts
        per_dashboard = c.execute(
            "SELECT dashboard_key, plan, COUNT(*) as cnt FROM subscriptions "
            "WHERE status = 'active' AND (expires_at IS NULL OR expires_at > ?) "
            "GROUP BY dashboard_key, plan ORDER BY dashboard_key", (now,)
        ).fetchall()
        return {
            "total": total,
            "active": active,
            "cancelled": cancelled,
            "expired": expired,
            "per_dashboard": per_dashboard,
        }


def create_enquiry(email: str, job_title: str, message: str) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO enquiries (email, job_title, message, created_at) VALUES (?, ?, ?, ?)",
            (email.strip(), job_title.strip(), message.strip(), int(time.time())),
        )
        return cur.lastrowid


def list_enquiries() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM enquiries ORDER BY created_at DESC").fetchall()


def get_enquiry_by_id(enquiry_id: int) -> Optional[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM enquiries WHERE id = ?", (enquiry_id,)).fetchone()


def mark_enquiry_read(enquiry_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE enquiries SET read = 1 WHERE id = ?", (enquiry_id,))


def count_unread_enquiries() -> int:
    with conn() as c:
        row = c.execute("SELECT COUNT(*) FROM enquiries WHERE read = 0").fetchone()
        return row[0] if row else 0


def mask_email(email: str) -> str:
    """Mask email like sh***@gmail.com."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.rsplit("@", 1)
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[:2]}***@{domain}"


# ── Password reset operations ────────────────────────────────────────────────

RESET_TTL = 60 * 60  # 1 hour


def create_password_reset(user_id: int) -> str:
    """Create a password reset token (expires in 1 hour). Returns the token."""
    token = secrets.token_urlsafe(36)
    now = int(time.time())
    with conn() as c:
        c.execute(
            "INSERT INTO password_resets (user_id, token, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, token, now, now + RESET_TTL),
        )
    return token


def get_password_reset(token: str) -> Optional[sqlite3.Row]:
    """Get a valid (not expired, not used) password reset record."""
    if not token:
        return None
    with conn() as c:
        return c.execute(
            "SELECT * FROM password_resets "
            "WHERE token = ? AND used = 0 AND expires_at > ?",
            (token, int(time.time())),
        ).fetchone()


def use_password_reset(token: str) -> None:
    """Mark a reset token as used."""
    with conn() as c:
        c.execute(
            "UPDATE password_resets SET used = 1 WHERE token = ?", (token,)
        )


def purge_expired_resets() -> int:
    """Delete expired or used reset tokens."""
    with conn() as c:
        cur = c.execute(
            "DELETE FROM password_resets WHERE expires_at <= ? OR used = 1",
            (int(time.time()),),
        )
        return cur.rowcount
