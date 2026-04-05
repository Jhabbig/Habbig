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
    email             TEXT UNIQUE NOT NULL,
    password_hash     TEXT NOT NULL,
    password_salt     TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    is_admin          INTEGER NOT NULL DEFAULT 0,
    default_dashboard TEXT
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

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id);
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


def create_user(email: str, password: str, is_admin: bool = False) -> int:
    email = email.lower().strip()
    pwd_hash, salt = _hash_password(password)
    with conn() as c:
        cur = c.execute(
            "INSERT INTO users (email, password_hash, password_salt, created_at, is_admin) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, pwd_hash, salt, int(time.time()), 1 if is_admin else 0),
        )
        return cur.lastrowid


def get_user_by_email(email: str) -> Optional[sqlite3.Row]:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
    return row


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
            "SELECT s.*, u.email, u.is_admin FROM sessions s "
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
