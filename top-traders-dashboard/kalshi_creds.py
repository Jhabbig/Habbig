#!/usr/bin/env python3
"""
Kalshi credential storage — Fernet-encrypted at rest.

Single-user dashboard so we keep one credential row keyed by user_id="default"
in DEV_MODE. When run behind the gateway, the gateway's user header drives the
key. The on-disk DB only ever holds the encrypted blob.

Encryption key resolution order:
  1. KALSHI_CRED_KEY env var (Fernet key, base64-urlsafe 32 bytes)
  2. .kalshi_master_key file next to this module (auto-generated for DEV_MODE)
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "kalshi_creds.sqlite3"
_KEY_FILE = Path(__file__).parent / ".kalshi_master_key"


# ─── Encryption key bootstrap ───────────────────────────────────────────

def _get_fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError(
            "The 'cryptography' package is required for Kalshi credential storage"
        ) from exc

    key = os.environ.get("KALSHI_CRED_KEY")
    if not key:
        if _KEY_FILE.exists():
            key = _KEY_FILE.read_text().strip()
        else:
            key = Fernet.generate_key().decode()
            _KEY_FILE.write_text(key)
            try:
                _KEY_FILE.chmod(0o600)
            except OSError:
                pass
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


# ─── Schema ─────────────────────────────────────────────────────────────

def _conn():
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    # Restrict DB file permissions on first creation
    try:
        if DB_PATH.exists():
            DB_PATH.chmod(0o600)
    except OSError:
        pass
    return c


def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS kalshi_credentials (
                user_id        TEXT PRIMARY KEY,
                api_key_hint   TEXT,           -- last 4 chars of API key for display
                cipher         BLOB NOT NULL,  -- Fernet ciphertext: api_key + private_key_pem JSON
                created_at     INTEGER NOT NULL,
                updated_at     INTEGER NOT NULL
            )
        """)


# ─── CRUD ───────────────────────────────────────────────────────────────

def save_creds(user_id: str, api_key: str, private_key_pem: str) -> None:
    """Encrypt and persist a Kalshi credential pair."""
    import json
    init_db()
    f = _get_fernet()
    blob = json.dumps({
        "api_key": api_key.strip(),
        "private_key_pem": private_key_pem.strip(),
    }).encode()
    cipher = f.encrypt(blob)
    hint = api_key.strip()[-4:] if len(api_key.strip()) >= 4 else "?"
    now = int(time.time())
    with _conn() as c:
        c.execute("""
            INSERT INTO kalshi_credentials (user_id, api_key_hint, cipher, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                api_key_hint = excluded.api_key_hint,
                cipher       = excluded.cipher,
                updated_at   = excluded.updated_at
        """, (user_id, hint, cipher, now, now))


def get_creds(user_id: str) -> Optional[dict]:
    """Decrypt and return {api_key, private_key_pem} or None."""
    import json
    import logging
    log = logging.getLogger("top-traders.kalshi-creds")
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT cipher FROM kalshi_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    f = _get_fernet()
    try:
        blob = f.decrypt(row["cipher"])
        return json.loads(blob)
    except Exception as e:
        log.warning("Failed to decrypt Kalshi credentials for user %s: %s", user_id, e)
        return None


def has_creds(user_id: str) -> bool:
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT api_key_hint, created_at, updated_at FROM kalshi_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row is not None


def get_status(user_id: str) -> dict:
    """Return non-sensitive connection metadata for the UI."""
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT api_key_hint, created_at, updated_at FROM kalshi_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"connected": False}
    return {
        "connected": True,
        "api_key_hint": row["api_key_hint"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def delete_creds(user_id: str) -> bool:
    init_db()
    with _conn() as c:
        cur = c.execute("DELETE FROM kalshi_credentials WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0
