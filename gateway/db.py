"""SQLite layer for the gateway — users, sessions, subscriptions."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

log = logging.getLogger("gateway.db")

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
    invite_token_id   INTEGER REFERENCES invite_tokens(id),
    -- has_full_access = "viewer pass": grants read access to every dashboard
    -- without admin privileges. Set when claiming an invite_token whose
    -- grants_full_access=1. Used for investor / partner / press passes.
    has_full_access   INTEGER NOT NULL DEFAULT 0
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
    claimed_at      INTEGER,
    -- grants_full_access = when claimed, the new user is marked
    -- has_full_access=1 (read-only access to every dashboard, no admin powers).
    grants_full_access INTEGER NOT NULL DEFAULT 0,
    -- stripe_sub_id = the subscription that paid for this viewer-pass token.
    -- Used to revoke access when the customer's subscription cancels.
    stripe_sub_id   TEXT
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

CREATE TABLE IF NOT EXISTS trading_credentials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    platform    TEXT NOT NULL,
    cred_data   TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE(user_id, platform),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trading_orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    platform         TEXT NOT NULL,
    market_slug      TEXT NOT NULL,
    market_question  TEXT NOT NULL DEFAULT '',
    side             TEXT NOT NULL,
    action           TEXT NOT NULL,
    amount           REAL NOT NULL,
    price            REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    error            TEXT,
    fill_price       REAL,
    fill_amount      REAL,
    order_ext_id     TEXT,
    source_dashboard TEXT,
    created_at       INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stripe_events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    processed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS superuser_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    dashboards      TEXT NOT NULL DEFAULT '',
    aspects         TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER,
    last_used_at    INTEGER,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS user_positions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    platform         TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    token_or_side    TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL DEFAULT '',
    qty_open         REAL NOT NULL DEFAULT 0,
    qty_closed       REAL NOT NULL DEFAULT 0,
    avg_entry_price  REAL NOT NULL DEFAULT 0,
    avg_exit_price   REAL,
    realized_pnl     REAL NOT NULL DEFAULT 0,
    fees_paid        REAL NOT NULL DEFAULT 0,
    last_mark_price  REAL,
    last_mark_at     INTEGER,
    status           TEXT NOT NULL DEFAULT 'open',
    source_dashboard TEXT,
    opened_at        INTEGER NOT NULL,
    closed_at        INTEGER,
    UNIQUE(user_id, platform, external_id, token_or_side),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subs_active ON subscriptions(user_id, dashboard_key, status);
CREATE INDEX IF NOT EXISTS idx_invite_token ON invite_tokens(token);
CREATE INDEX IF NOT EXISTS idx_invite_status ON invite_tokens(status);
CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token);
CREATE INDEX IF NOT EXISTS idx_trading_creds_user ON trading_credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_trading_orders_user ON trading_orders(user_id);
CREATE INDEX IF NOT EXISTS idx_stripe_events_processed ON stripe_events(processed_at);
CREATE INDEX IF NOT EXISTS idx_positions_user ON user_positions(user_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON user_positions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_positions_platform ON user_positions(user_id, platform);
CREATE INDEX IF NOT EXISTS idx_superuser_keys ON superuser_keys(key);
CREATE INDEX IF NOT EXISTS idx_superuser_active ON superuser_keys(active);
"""


def _configure_connection(c: sqlite3.Connection) -> None:
    """Apply performance pragmas to a fresh connection."""
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA synchronous = NORMAL")
    c.execute("PRAGMA cache_size = -8000")  # 8 MB page cache
    c.execute("PRAGMA busy_timeout = 5000")  # wait up to 5 s on lock


# Thread-local connection pool: one persistent connection per thread instead
# of opening and closing on every query.  Eliminates ~3-5 connect/close cycles
# per proxied request.
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Return the thread-local SQLite connection, creating it if needed."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        _configure_connection(c)
        _local.conn = c
    return c


@contextmanager
def conn():
    c = _get_conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


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
        if "username" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN username TEXT")
            for row in c.execute("SELECT id, email FROM users WHERE username IS NULL").fetchall():
                uname = row[1].split("@")[0] if row[1] else f"user{row[0]}"
                c.execute("UPDATE users SET username = ? WHERE id = ?", (uname, row[0]))
        if "has_full_access" not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN has_full_access INTEGER NOT NULL DEFAULT 0")
        # invite_tokens migrations
        invite_cols = {row["name"] for row in c.execute("PRAGMA table_info(invite_tokens)")}
        if "target_email" not in invite_cols:
            c.execute("ALTER TABLE invite_tokens ADD COLUMN target_email TEXT")
        if "grants_full_access" not in invite_cols:
            c.execute("ALTER TABLE invite_tokens ADD COLUMN grants_full_access INTEGER NOT NULL DEFAULT 0")
        if "stripe_sub_id" not in invite_cols:
            c.execute("ALTER TABLE invite_tokens ADD COLUMN stripe_sub_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_invite_tokens_stripe_sub_id ON invite_tokens(stripe_sub_id)")
        # subscriptions migrations
        sub_cols = {row["name"] for row in c.execute("PRAGMA table_info(subscriptions)")}
        if "stripe_sub_id" not in sub_cols:
            c.execute("ALTER TABLE subscriptions ADD COLUMN stripe_sub_id TEXT")
        if "source" not in sub_cols:
            c.execute("ALTER TABLE subscriptions ADD COLUMN source TEXT NOT NULL DEFAULT 'placeholder'")
        # trading_orders migrations
        order_cols = {row["name"] for row in c.execute("PRAGMA table_info(trading_orders)")}
        if "source_dashboard" not in order_cols:
            c.execute("ALTER TABLE trading_orders ADD COLUMN source_dashboard TEXT")


# ── Password hashing ──────────────────────────────────────────────────────────
# Using PBKDF2-HMAC-SHA256 (stdlib, no external deps). 600k iterations per
# OWASP 2023 recommendation for SHA-256.

_PBKDF2_ITERATIONS = 600_000


def _hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if len(password) > 256:
        raise ValueError("Password too long")
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return dk.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    candidate, _ = _hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def verify_user_password(email: str, password: str) -> bool:
    """Look up user by email and verify their password."""
    user = get_user_by_email(email)
    if not user:
        return False
    return verify_password(password, user["password_hash"], user["password_salt"])


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


def delete_user(user_id: int) -> None:
    """Delete a user by ID and all related rows.

    Used both to clean up orphaned users on failed invite claim and to fully
    purge a user. We don't rely on FK CASCADE because the schema was created
    without `PRAGMA foreign_keys = ON` enforcement, so we explicitly cascade
    to children tables here within a single transaction.
    """
    with conn() as c:
        # Sessions for this user
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        # Trading credentials and orders
        try:
            c.execute("DELETE FROM trading_credentials WHERE user_id = ?", (user_id,))
        except sqlite3.OperationalError:
            pass  # table may not exist in older deployments
        try:
            c.execute("DELETE FROM trading_orders WHERE user_id = ?", (user_id,))
        except sqlite3.OperationalError:
            pass
        # Subscriptions
        try:
            c.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        except sqlite3.OperationalError:
            pass
        # Password reset tokens
        try:
            c.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
        except sqlite3.OperationalError:
            pass
        # Detach claimed invite tokens (don't delete — preserves audit trail
        # but clears the FK so we don't have rows pointing at a deleted user)
        try:
            c.execute(
                "UPDATE invite_tokens SET claimed_by_user_id = NULL WHERE claimed_by_user_id = ?",
                (user_id,),
            )
        except sqlite3.OperationalError:
            pass
        # Finally delete the user row
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))


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


def update_user_password(user_id: int, new_password: str) -> None:
    """Hash and update a user's password."""
    pwd_hash, salt = _hash_password(new_password)
    with conn() as c:
        c.execute(
            "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
            (pwd_hash, salt, user_id),
        )


def update_user_email(user_id: int, new_email: str) -> None:
    """Update a user's email address."""
    with conn() as c:
        c.execute(
            "UPDATE users SET email = ? WHERE id = ?",
            (new_email.lower().strip(), user_id),
        )


def link_invite_token_to_user(user_id: int, token_str: str) -> None:
    """Set the invite_token_id on a user from a token string."""
    with conn() as c:
        c.execute(
            "UPDATE users SET invite_token_id = "
            "(SELECT id FROM invite_tokens WHERE token = ?) WHERE id = ?",
            (token_str, user_id),
        )


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
            "SELECT s.*, u.username, u.email, u.is_admin, u.suspended, "
            "u.has_full_access "
            "FROM sessions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires_at > ?",
            (token, int(time.time())),
        ).fetchone()
    return row


def delete_session(token: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


def delete_user_sessions(user_id: int) -> None:
    """Delete all sessions for a user (used on password reset, suspension)."""
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


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
        # Single query: admin OR full-access viewer OR active subscription.
        row = c.execute(
            "SELECT 1 FROM users "
            "WHERE id = ? AND (is_admin > 0 OR has_full_access > 0) "
            "UNION ALL "
            "SELECT 1 FROM subscriptions "
            "WHERE user_id = ? AND dashboard_key = ? AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "LIMIT 1",
            (user_id, user_id, dashboard_key, now),
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


def cancel_subscription_by_stripe_id(stripe_sub_id: str) -> None:
    """Cancel all subscriptions with the given Stripe subscription ID."""
    with conn() as c:
        c.execute(
            "UPDATE subscriptions SET status = 'cancelled' WHERE stripe_sub_id = ?",
            (stripe_sub_id,),
        )


# ── Invite token operations ──────────────────────────────────────────────────


def generate_invite_token() -> str:
    """Generate a 32-character URL-safe random invite token."""
    return secrets.token_urlsafe(24)


def create_invite_token(
    note: str = "",
    target_email: str = "",
    grants_full_access: bool = False,
    stripe_sub_id: str = "",
) -> str:
    """Create a new unclaimed invite token. Returns the token string.

    If grants_full_access=True, claiming this token marks the new user
    as has_full_access=1 (read-only access to every dashboard, no admin
    powers). Use for investor / partner / press passes.

    If stripe_sub_id is non-empty, the token is linked to that Stripe
    subscription so the gateway can revoke access when the subscription
    cancels.
    """
    token = generate_invite_token()
    with conn() as c:
        c.execute(
            "INSERT INTO invite_tokens "
            "(token, status, note, target_email, created_at, grants_full_access, stripe_sub_id) "
            "VALUES (?, 'unclaimed', ?, ?, ?, ?, ?)",
            (
                token,
                note,
                target_email.strip() or None,
                int(time.time()),
                1 if grants_full_access else 0,
                stripe_sub_id.strip() or None,
            ),
        )
    return token


def get_invite_token(token: str) -> Optional[sqlite3.Row]:
    token = token.strip()
    with conn() as c:
        return c.execute("SELECT * FROM invite_tokens WHERE token = ?", (token,)).fetchone()


def find_invite_token_by_stripe_sub(stripe_sub_id: str) -> Optional[sqlite3.Row]:
    """Look up the invite token tied to a Stripe subscription.

    Used by the webhook handler when a subscription cancels — we need to
    find the token (and the user it claimed for) to revoke their pass.
    """
    if not stripe_sub_id:
        return None
    with conn() as c:
        return c.execute(
            "SELECT * FROM invite_tokens WHERE stripe_sub_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (stripe_sub_id,),
        ).fetchone()


def claim_invite_token(token_str: str, user_id: int, email: str) -> bool:
    """Atomically claim a token. Returns True if claimed, False if already claimed (race condition).

    If the token's grants_full_access=1, also sets users.has_full_access=1
    so the user immediately sees every dashboard as accessible (without
    admin powers).
    """
    token_str = token_str.strip()
    with conn() as c:
        cur = c.execute(
            "UPDATE invite_tokens SET status = 'claimed', claimed_by_user_id = ?, "
            "claimed_by_email = ?, claimed_at = ? WHERE token = ? AND status = 'unclaimed'",
            (user_id, email, int(time.time()), token_str),
        )
        if cur.rowcount == 0:
            return False
        c.execute("UPDATE users SET invite_token_id = (SELECT id FROM invite_tokens WHERE token = ?) WHERE id = ?",
                   (token_str, user_id))
        # If this token grants full-access, flip the user's has_full_access flag.
        c.execute(
            "UPDATE users SET has_full_access = 1 "
            "WHERE id = ? AND EXISTS ("
            "    SELECT 1 FROM invite_tokens WHERE token = ? AND grants_full_access = 1"
            ")",
            (user_id, token_str),
        )
        return True


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


def set_user_has_full_access(user_id: int, has_full_access: bool) -> None:
    """Toggle the viewer-pass flag on a user.

    has_full_access=True grants read access to every dashboard without
    admin privileges. False removes that grant; the user falls back to
    their actual subscription set.
    """
    with conn() as c:
        c.execute(
            "UPDATE users SET has_full_access = ? WHERE id = ?",
            (1 if has_full_access else 0, user_id),
        )


def set_user_admin(user_id: int, is_admin: bool) -> None:
    """Legacy helper — promotes to admin (1) or demotes to user (0)."""
    set_user_role(user_id, 1 if is_admin else 0)


def set_user_suspended(user_id: int, suspended: bool) -> None:
    with conn() as c:
        c.execute("UPDATE users SET suspended = ? WHERE id = ?", (1 if suspended else 0, user_id))
        if suspended:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def list_all_subscriptions() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT s.*, u.email, u.username FROM subscriptions s "
            "JOIN users u ON u.id = s.user_id "
            "ORDER BY s.started_at DESC"
        ).fetchall()


def stripe_event_already_processed(event_id: str) -> bool:
    """Idempotency check for Stripe webhook delivery — True if already handled."""
    if not event_id:
        return False
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM stripe_events WHERE event_id = ?", (event_id,)
        ).fetchone()
    return row is not None


def mark_stripe_event_processed(event_id: str, event_type: str) -> bool:
    """Atomically record that we've processed a Stripe event.
    Returns True on first insertion, False if the event was already recorded
    (i.e., a concurrent webhook delivery beat us to it)."""
    if not event_id:
        return True
    try:
        with conn() as c:
            c.execute(
                "INSERT INTO stripe_events (event_id, event_type, processed_at) VALUES (?, ?, ?)",
                (event_id, event_type or "", int(time.time())),
            )
        return True
    except sqlite3.IntegrityError:
        return False


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


# ── Encryption for trading credentials ──────────────────────────────────────

_TRADING_KEY_ENV = "TRADING_ENCRYPTION_KEY"
_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get(_TRADING_KEY_ENV, "")
        if not key:
            if os.getenv("PRODUCTION", "0") == "1":
                raise RuntimeError(
                    "TRADING_ENCRYPTION_KEY must be set in production. "
                    "Generate with: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
                )
            if os.getenv("DEV_MODE", "").strip() != "1":
                raise RuntimeError(
                    "TRADING_ENCRYPTION_KEY not set. Set DEV_MODE=1 to use an ephemeral key for development."
                )
            key = Fernet.generate_key().decode()
            log.warning(
                "%s not set — using ephemeral key (DEV_MODE). "
                "Trading credentials will NOT survive restart.",
                _TRADING_KEY_ENV,
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def _encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()


# ── Trading credential operations ────────────────────────────────────────────


def save_trading_credentials(user_id: int, platform: str, creds: dict) -> None:
    """Encrypt and store trading credentials for a platform."""
    now = int(time.time())
    encrypted = _encrypt(json.dumps(creds))
    with conn() as c:
        c.execute(
            """
            INSERT INTO trading_credentials (user_id, platform, cred_data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, platform) DO UPDATE SET
                cred_data  = excluded.cred_data,
                updated_at = excluded.updated_at
            """,
            (user_id, platform, encrypted, now, now),
        )


def get_trading_credentials(user_id: int, platform: str) -> Optional[dict]:
    """Retrieve and decrypt trading credentials. Returns None if not configured."""
    with conn() as c:
        row = c.execute(
            "SELECT cred_data FROM trading_credentials WHERE user_id = ? AND platform = ?",
            (user_id, platform),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(_decrypt(row["cred_data"]))
    except Exception as e:
        log.error("Failed to decrypt trading credentials for user %s, platform %s: %s", user_id, platform, e)
        return None


def has_trading_credentials(user_id: int) -> dict[str, bool]:
    """Return which platforms have credentials configured."""
    with conn() as c:
        rows = c.execute(
            "SELECT platform FROM trading_credentials WHERE user_id = ?", (user_id,)
        ).fetchall()
    platforms = {r["platform"] for r in rows}
    return {
        "polymarket": "polymarket" in platforms,
        "kalshi": "kalshi" in platforms,
        "alpaca": "alpaca" in platforms,
    }


def delete_trading_credentials(user_id: int, platform: str) -> None:
    with conn() as c:
        c.execute(
            "DELETE FROM trading_credentials WHERE user_id = ? AND platform = ?",
            (user_id, platform),
        )


# ── Trading order operations ─────────────────────────────────────────────────


def create_trading_order(
    user_id: int, platform: str, market_slug: str, market_question: str,
    side: str, action: str, amount: float, price: float,
    source_dashboard: str | None = None,
) -> int:
    """Create a pending order record. Returns the order ID."""
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "INSERT INTO trading_orders "
            "(user_id, platform, market_slug, market_question, side, action, amount, price, status, source_dashboard, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (user_id, platform, market_slug, market_question, side, action, amount, price, source_dashboard, now),
        )
        return cur.lastrowid


_TRADING_ORDER_FIELDS = {"status", "error", "fill_price", "fill_amount", "order_ext_id"}


def update_trading_order(order_id: int, **fields) -> None:
    """Update an order with fill/error info."""
    if not fields:
        return
    bad = set(fields) - _TRADING_ORDER_FIELDS
    if bad:
        raise ValueError(f"disallowed fields: {bad}")
    # Double-check column names are simple identifiers (defense in depth)
    for k in fields:
        if not k.isidentifier():
            raise ValueError(f"invalid column name: {k!r}")
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [order_id]
    with conn() as c:
        c.execute(f"UPDATE trading_orders SET {sets} WHERE id = ?", vals)


def get_recent_orders(user_id: int, limit: int = 20) -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute(
            "SELECT * FROM trading_orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


# ── Position operations ────────────────────────────────────────────────────────


def upsert_position(
    user_id: int,
    platform: str,
    external_id: str,
    token_or_side: str,
    *,
    title: str = "",
    qty_open: float = 0,
    qty_closed: float = 0,
    avg_entry_price: float = 0,
    avg_exit_price: float | None = None,
    realized_pnl: float = 0,
    fees_paid: float = 0,
    last_mark_price: float | None = None,
    last_mark_at: int | None = None,
    status: str = "open",
    source_dashboard: str | None = None,
    opened_at: int | None = None,
    closed_at: int | None = None,
) -> int:
    """Insert or update a position. Returns the row ID."""
    now = int(time.time())
    if opened_at is None:
        opened_at = now
    with conn() as c:
        cur = c.execute(
            """
            INSERT INTO user_positions
                (user_id, platform, external_id, token_or_side, title,
                 qty_open, qty_closed, avg_entry_price, avg_exit_price,
                 realized_pnl, fees_paid, last_mark_price, last_mark_at,
                 status, source_dashboard, opened_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, platform, external_id, token_or_side) DO UPDATE SET
                title           = excluded.title,
                qty_open        = excluded.qty_open,
                qty_closed      = excluded.qty_closed,
                avg_entry_price = excluded.avg_entry_price,
                avg_exit_price  = COALESCE(excluded.avg_exit_price, user_positions.avg_exit_price),
                realized_pnl    = excluded.realized_pnl,
                fees_paid       = excluded.fees_paid,
                last_mark_price = COALESCE(excluded.last_mark_price, user_positions.last_mark_price),
                last_mark_at    = COALESCE(excluded.last_mark_at, user_positions.last_mark_at),
                status          = excluded.status,
                source_dashboard = COALESCE(excluded.source_dashboard, user_positions.source_dashboard),
                closed_at       = excluded.closed_at
            """,
            (
                user_id, platform, external_id, token_or_side, title,
                qty_open, qty_closed, avg_entry_price, avg_exit_price,
                realized_pnl, fees_paid, last_mark_price, last_mark_at,
                status, source_dashboard, opened_at, closed_at,
            ),
        )
        return cur.lastrowid


def update_mark_price(position_id: int, mark_price: float) -> None:
    """Update the mark-to-market price for a position."""
    now = int(time.time())
    with conn() as c:
        c.execute(
            "UPDATE user_positions SET last_mark_price = ?, last_mark_at = ? WHERE id = ?",
            (mark_price, now, position_id),
        )


def get_open_positions(user_id: int, platform: str | None = None) -> list[sqlite3.Row]:
    """Fetch open positions, optionally filtered by platform."""
    with conn() as c:
        if platform:
            return c.execute(
                "SELECT * FROM user_positions WHERE user_id = ? AND platform = ? AND status = 'open' ORDER BY opened_at DESC",
                (user_id, platform),
            ).fetchall()
        return c.execute(
            "SELECT * FROM user_positions WHERE user_id = ? AND status = 'open' ORDER BY opened_at DESC",
            (user_id,),
        ).fetchall()


def get_closed_positions(user_id: int, platform: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    """Fetch closed/settled positions."""
    with conn() as c:
        if platform:
            return c.execute(
                "SELECT * FROM user_positions WHERE user_id = ? AND platform = ? AND status != 'open' ORDER BY closed_at DESC LIMIT ?",
                (user_id, platform, limit),
            ).fetchall()
        return c.execute(
            "SELECT * FROM user_positions WHERE user_id = ? AND status != 'open' ORDER BY closed_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def get_all_positions(user_id: int) -> list[sqlite3.Row]:
    """All positions for a user (any status)."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM user_positions WHERE user_id = ? ORDER BY opened_at DESC",
            (user_id,),
        ).fetchall()


def get_positions_needing_mark(limit: int = 500) -> list[sqlite3.Row]:
    """Open positions across all users that need a price update.

    Used by the mark-to-market background worker.
    Returns distinct (platform, external_id, token_or_side) + position id.
    """
    with conn() as c:
        return c.execute(
            """
            SELECT id, user_id, platform, external_id, token_or_side
            FROM user_positions
            WHERE status = 'open' AND qty_open > 0
            ORDER BY last_mark_at ASC NULLS FIRST
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def get_portfolio_summary(user_id: int) -> dict:
    """Aggregate stats across all positions for a user."""
    with conn() as c:
        row = c.execute(
            """
            SELECT
                COUNT(CASE WHEN status = 'open' THEN 1 END)          AS open_count,
                COALESCE(SUM(CASE WHEN status = 'open'
                    THEN qty_open * COALESCE(last_mark_price, avg_entry_price)
                    END), 0)                                          AS open_value,
                COALESCE(SUM(CASE WHEN status = 'open'
                    THEN qty_open * (COALESCE(last_mark_price, avg_entry_price) - avg_entry_price)
                    END), 0)                                          AS unrealized_pnl,
                COALESCE(SUM(realized_pnl), 0)                        AS realized_pnl,
                COALESCE(SUM(fees_paid), 0)                           AS total_fees,
                COUNT(CASE WHEN status IN ('closed', 'settled_win', 'settled_loss') THEN 1 END) AS closed_count,
                COUNT(CASE WHEN status = 'settled_win' THEN 1 END)    AS wins,
                COUNT(CASE WHEN status = 'settled_loss' THEN 1 END)   AS losses
            FROM user_positions WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    if not row:
        return {
            "open_count": 0, "open_value": 0, "unrealized_pnl": 0,
            "realized_pnl": 0, "total_fees": 0, "closed_count": 0,
            "wins": 0, "losses": 0, "net_pnl": 0,
        }
    r = dict(row)
    r["net_pnl"] = round(r["realized_pnl"] + r["unrealized_pnl"] - r["total_fees"], 2)
    for k in ("open_value", "unrealized_pnl", "realized_pnl", "total_fees"):
        r[k] = round(r[k], 2)
    return r


def get_portfolio_by_platform(user_id: int) -> list[dict]:
    """Aggregate stats grouped by platform."""
    with conn() as c:
        rows = c.execute(
            """
            SELECT
                platform,
                COUNT(CASE WHEN status = 'open' THEN 1 END) AS open_count,
                COALESCE(SUM(realized_pnl), 0)               AS realized_pnl,
                COALESCE(SUM(CASE WHEN status = 'open'
                    THEN qty_open * (COALESCE(last_mark_price, avg_entry_price) - avg_entry_price)
                    END), 0)                                  AS unrealized_pnl,
                COALESCE(SUM(fees_paid), 0)                   AS total_fees
            FROM user_positions WHERE user_id = ?
            GROUP BY platform
            """,
            (user_id,),
        ).fetchall()
    return [
        {**dict(r), "net_pnl": round(r["realized_pnl"] + r["unrealized_pnl"] - r["total_fees"], 2)}
        for r in rows
    ]


def get_portfolio_by_dashboard(user_id: int) -> list[dict]:
    """Aggregate stats grouped by source_dashboard."""
    with conn() as c:
        rows = c.execute(
            """
            SELECT
                COALESCE(source_dashboard, 'unknown') AS dashboard,
                platform,
                COUNT(CASE WHEN status = 'open' THEN 1 END) AS open_count,
                COALESCE(SUM(realized_pnl), 0)               AS realized_pnl,
                COALESCE(SUM(CASE WHEN status = 'open'
                    THEN qty_open * (COALESCE(last_mark_price, avg_entry_price) - avg_entry_price)
                    END), 0)                                  AS unrealized_pnl,
                COALESCE(SUM(fees_paid), 0)                   AS total_fees
            FROM user_positions WHERE user_id = ?
            GROUP BY source_dashboard, platform
            """,
            (user_id,),
        ).fetchall()
    return [
        {**dict(r), "net_pnl": round(r["realized_pnl"] + r["unrealized_pnl"] - r["total_fees"], 2)}
        for r in rows
    ]


def rebuild_positions_for_user(user_id: int) -> int:
    """Derive positions from the trading_orders audit log.

    Groups filled orders by (platform, market_slug, side) and computes
    net qty, VWAP entry/exit, realized P&L. Upserts into user_positions.

    Returns the number of positions upserted.
    """
    with conn() as c:
        orders = c.execute(
            """
            SELECT platform, market_slug, market_question, side, action,
                   fill_price, fill_amount, created_at
            FROM trading_orders
            WHERE user_id = ? AND status = 'submitted' AND fill_price IS NOT NULL AND fill_amount IS NOT NULL
            ORDER BY created_at ASC
            """,
            (user_id,),
        ).fetchall()

    # Aggregate: key = (platform, market_slug, side)
    agg: dict[tuple, dict] = {}
    for o in orders:
        key = (o["platform"], o["market_slug"], o["side"])
        if key not in agg:
            agg[key] = {
                "question": o["market_question"],
                "buy_qty": 0, "buy_cost": 0,
                "sell_qty": 0, "sell_proceeds": 0,
                "first_at": o["created_at"],
                "last_at": o["created_at"],
            }
        a = agg[key]
        qty = float(o["fill_amount"])
        price = float(o["fill_price"])
        if o["action"] == "buy":
            a["buy_qty"] += qty
            a["buy_cost"] += qty * price
        else:
            a["sell_qty"] += qty
            a["sell_proceeds"] += qty * price
        a["last_at"] = o["created_at"]

    count = 0
    for (platform, slug, side), a in agg.items():
        net = a["buy_qty"] - a["sell_qty"]
        avg_entry = (a["buy_cost"] / a["buy_qty"]) if a["buy_qty"] > 0 else 0
        avg_exit = (a["sell_proceeds"] / a["sell_qty"]) if a["sell_qty"] > 0 else None
        realized = a["sell_proceeds"] - (a["sell_qty"] * avg_entry) if a["sell_qty"] > 0 else 0

        status = "open" if net > 0.001 else "closed"
        upsert_position(
            user_id=user_id,
            platform=platform,
            external_id=slug,
            token_or_side=side,
            title=a["question"],
            qty_open=max(net, 0),
            qty_closed=a["sell_qty"],
            avg_entry_price=round(avg_entry, 4),
            avg_exit_price=round(avg_exit, 4) if avg_exit is not None else None,
            realized_pnl=round(realized, 4),
            status=status,
            opened_at=a["first_at"],
            closed_at=a["last_at"] if status == "closed" else None,
        )
        count += 1
    return count


# ── Stripe webhook event purge ─────────────────────────────────────────────


def purge_old_stripe_events(older_than_days: int = 90) -> int:
    """Drop processed Stripe event rows older than the cutoff. Stripe retries
    top out at ~3 days; 90 days is generous and keeps the table small."""
    cutoff = int(time.time()) - (older_than_days * 86400)
    with conn() as c:
        cur = c.execute(
            "DELETE FROM stripe_events WHERE processed_at < ?",
            (cutoff,),
        )
        return cur.rowcount


# ── Superuser key operations ─────────────────────────────────────────────────

def generate_superuser_key() -> str:
    """Generate a 32-character URL-safe random superuser key."""
    return secrets.token_urlsafe(24)


def create_superuser_key(name: str, dashboards: list[str] | None = None, expires_in_days: int | None = None, custom_key: str | None = None, aspects: list[str] | None = None) -> str:
    """Create a new superuser key.

    Args:
        name: Human-readable name for the key
        dashboards: List of dashboard keys this superuser can access (empty = all)
        expires_in_days: Number of days until key expires (None = never expires)
        custom_key: Optional custom key string (e.g., "Julian-habbig"). If not provided, generates random.
        aspects: List of aspect strings (e.g., ["read-only", "no-trading", "demo-mode"])

    Returns:
        The generated superuser key
    """
    key = custom_key if custom_key else generate_superuser_key()

    # Validate custom key if provided
    if custom_key:
        if not custom_key.strip():
            raise ValueError("Custom key cannot be empty")
        if len(custom_key) < 3:
            raise ValueError("Custom key must be at least 3 characters")
        # Allow alphanumeric, hyphens, underscores
        if not all(c.isalnum() or c in ('-', '_') for c in custom_key):
            raise ValueError("Custom key can only contain alphanumeric characters, hyphens, and underscores")

    now = int(time.time())
    expires_at = None
    if expires_in_days is not None:
        expires_at = now + (expires_in_days * 86400)

    dashboards_str = ",".join(dashboards) if dashboards else ""
    aspects_str = ",".join(aspects) if aspects else ""

    with conn() as c:
        c.execute(
            """INSERT INTO superuser_keys (key, name, dashboards, aspects, created_at, expires_at, active)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (key, name, dashboards_str, aspects_str, now, expires_at),
        )
    return key


def validate_superuser_key(key: str) -> dict | None:
    """Check if a superuser key is valid and active.

    Returns:
        dict with key info if valid, None otherwise
    """
    now = int(time.time())
    with conn() as c:
        row = c.execute(
            """SELECT id, name, dashboards, aspects, created_at, expires_at, last_used_at
               FROM superuser_keys
               WHERE key = ? AND active = 1
               AND (expires_at IS NULL OR expires_at > ?)""",
            (key, now),
        ).fetchone()

    if row is None:
        return None

    # Update last_used_at
    with conn() as c:
        c.execute("UPDATE superuser_keys SET last_used_at = ? WHERE key = ?", (now, key))

    return {
        "id": row["id"],
        "name": row["name"],
        "dashboards": [d.strip() for d in row["dashboards"].split(",") if d.strip()],
        "aspects": [a.strip() for a in row["aspects"].split(",") if a.strip()],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "last_used_at": row["last_used_at"],
    }


def has_superuser_key_access(key: str, dashboard_key: str) -> bool:
    """Check if a superuser key grants access to a specific dashboard."""
    key_info = validate_superuser_key(key)
    if key_info is None:
        return False

    # Empty dashboards list means access to all dashboards
    if not key_info["dashboards"]:
        return True

    return dashboard_key in key_info["dashboards"]


def key_has_aspect(key: str, aspect: str) -> bool:
    """Check if a superuser key has a specific aspect."""
    key_info = validate_superuser_key(key)
    if key_info is None:
        return False
    return aspect.lower().strip() in [a.lower() for a in key_info["aspects"]]


def get_key_aspects(key: str) -> list[str]:
    """Get all aspects for a superuser key."""
    key_info = validate_superuser_key(key)
    if key_info is None:
        return []
    return key_info["aspects"]


def list_superuser_keys() -> list[dict]:
    """List all superuser keys (excluding the actual key values)."""
    with conn() as c:
        rows = c.execute(
            """SELECT id, name, dashboards, aspects, created_at, expires_at, last_used_at, active
               FROM superuser_keys
               ORDER BY created_at DESC"""
        ).fetchall()

    return [
        {
            "id": row["id"],
            "name": row["name"],
            "dashboards": [d.strip() for d in row["dashboards"].split(",") if d.strip()],
            "aspects": [a.strip() for a in row["aspects"].split(",") if a.strip()],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "last_used_at": row["last_used_at"],
            "active": bool(row["active"]),
        }
        for row in rows
    ]


def revoke_superuser_key(key_id: int) -> bool:
    """Revoke a superuser key by ID. Returns True if successful."""
    with conn() as c:
        c.execute("UPDATE superuser_keys SET active = 0 WHERE id = ?", (key_id,))
        return c.total_changes > 0


def toggle_superuser_key(key_id: int) -> dict | None:
    """Toggle a superuser key's active status. Returns updated key info or None if not found."""
    with conn() as c:
        # Get current status
        row = c.execute(
            "SELECT id, active FROM superuser_keys WHERE id = ?",
            (key_id,),
        ).fetchone()

        if row is None:
            return None

        # Toggle status
        new_active = 1 if not row["active"] else 0
        c.execute(
            "UPDATE superuser_keys SET active = ? WHERE id = ?",
            (new_active, key_id),
        )

        # Return updated info
        updated = c.execute(
            """SELECT id, name, dashboards, aspects, created_at, expires_at, last_used_at, active
               FROM superuser_keys WHERE id = ?""",
            (key_id,),
        ).fetchone()

        return {
            "id": updated["id"],
            "name": updated["name"],
            "dashboards": [d.strip() for d in updated["dashboards"].split(",") if d.strip()],
            "aspects": [a.strip() for a in updated["aspects"].split(",") if a.strip()],
            "created_at": updated["created_at"],
            "expires_at": updated["expires_at"],
            "last_used_at": updated["last_used_at"],
            "active": bool(updated["active"]),
        }


def enable_superuser_key(key_id: int) -> bool:
    """Enable a superuser key by ID. Returns True if successful."""
    with conn() as c:
        c.execute("UPDATE superuser_keys SET active = 1 WHERE id = ?", (key_id,))
        return c.total_changes > 0


def disable_superuser_key(key_id: int) -> bool:
    """Disable a superuser key by ID. Returns True if successful."""
    with conn() as c:
        c.execute("UPDATE superuser_keys SET active = 0 WHERE id = ?", (key_id,))
        return c.total_changes > 0
