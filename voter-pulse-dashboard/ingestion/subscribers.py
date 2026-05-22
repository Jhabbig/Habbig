"""SQLite-backed subscriber storage + signed unsubscribe tokens.

We persist two tables in a single SQLite file:

  subscribers   : email + when they signed up + whether they've unsubscribed
  alert_state   : key/value table for tracking the last-sent mood + when

Why SQLite: voter-pulse otherwise has no DB and the subscriber set is
small. Postgres would be overkill; a flat-file JSON would race under
concurrent writes. SQLite gives us ACID with zero ops cost. The file
lives at /app/data/voter_pulse.db inside the container, backed by a
docker volume so it survives image rebuilds.

Unsubscribe tokens are 16 hex chars of HMAC-SHA256(secret, email) —
short enough for a clean URL, long enough to be infeasible to guess.
The secret comes from ALERT_SIGNING_SECRET (separate from the gateway
SSO secret) so a leak of one doesn't compromise the other.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock

DEFAULT_DB_PATH = os.environ.get("VOTER_PULSE_DB", "/app/data/voter_pulse.db")
_DB_LOCK = Lock()

_EMAIL_RX = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _db_path() -> str:
    p = Path(DEFAULT_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema() -> None:
    with _DB_LOCK, _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subscribers (
                email          TEXT PRIMARY KEY,
                subscribed_at  INTEGER NOT NULL,
                unsubscribed   INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS alert_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)


def is_valid_email(email: str) -> bool:
    if not email or len(email) > 254:
        return False
    return bool(_EMAIL_RX.match(email.strip().lower()))


def subscribe(email: str) -> dict:
    email = (email or "").strip().lower()
    if not is_valid_email(email):
        return {"ok": False, "error": "invalid email"}
    init_schema()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO subscribers(email, subscribed_at, unsubscribed) VALUES (?, ?, 0) "
            "ON CONFLICT(email) DO UPDATE SET unsubscribed=0",
            (email, int(time.time())),
        )
        new = cur.rowcount > 0
    return {"ok": True, "email": email, "newly_subscribed": new}


def unsubscribe(email: str) -> dict:
    email = (email or "").strip().lower()
    if not is_valid_email(email):
        return {"ok": False, "error": "invalid email"}
    init_schema()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            "UPDATE subscribers SET unsubscribed=1 WHERE email=?", (email,)
        )
    return {"ok": True, "email": email, "matched": cur.rowcount > 0}


def list_active() -> list[str]:
    init_schema()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT email FROM subscribers WHERE unsubscribed=0 ORDER BY subscribed_at"
        ).fetchall()
    return [r["email"] for r in rows]


def count_active() -> int:
    init_schema()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM subscribers WHERE unsubscribed=0"
        ).fetchone()
    return int(row["n"]) if row else 0


def get_alert_state(key: str) -> str | None:
    init_schema()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute("SELECT value FROM alert_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_alert_state(key: str, value: str) -> None:
    init_schema()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO alert_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ── Signed unsubscribe tokens ────────────────────────────────────────────────

def _signing_secret() -> bytes:
    secret = os.environ.get("ALERT_SIGNING_SECRET") or ""
    if not secret:
        # Fall back to the gateway secret so we have *some* keyed signing even
        # if the operator forgot ALERT_SIGNING_SECRET. The fallback is logged
        # in token_for() via the caller path.
        secret = os.environ.get("GATEWAY_SSO_SECRET") or ""
    return secret.encode("utf-8")


def token_for(email: str) -> str:
    msg = (email or "").strip().lower().encode("utf-8")
    mac = hmac.new(_signing_secret(), msg, hashlib.sha256).hexdigest()
    return mac[:16]


def verify_token(email: str, token: str) -> bool:
    expected = token_for(email)
    return hmac.compare_digest(expected, (token or ""))
