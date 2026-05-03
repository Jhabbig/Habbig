"""Encrypted per-user Kalshi API key storage.

Why encrypt at rest:
  Each user submits their API key id + RSA private key (PEM) — the **private
  key is the bearer of trade authority** for that user's Kalshi account. A
  database leak would let an attacker place orders on every user's behalf.
  We encrypt with a master key the operator supplies via env; the DB on disk
  is then useless without the env-var.

Crypto choice:
  ``cryptography.fernet`` (AES-128-CBC + HMAC-SHA-256, authenticated). Fernet
  is the right primitive: high-level, hard to misuse, includes an integrity
  MAC so a tampered ciphertext is rejected at decrypt time. The master key
  is supplied via the ``CB_KEY_STORE_SECRET`` env var (urlsafe-base64).

Storage:
  Local SQLite at ``data/key_store.db``. Schema is two columns plus the
  encrypted blob — we never store anything in plaintext, not even the API
  key id (which by itself is innocuous, but uniformity is simpler than
  case-splitting and keeps keys/values opaque to anyone with read access).

Mode toggle:
  Each user has a ``mode`` of ``"paper"`` or ``"prod"``. Default ``"paper"``.
  Switching to prod requires an explicit user action and shows a banner.

We do NOT keep decrypted material in memory beyond a single signing/order
call. Every helper here re-decrypts on each call.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "key_store.db"
SECRET_ENV = "CB_KEY_STORE_SECRET"

_lock = Lock()


def _get_master_key() -> bytes:
    """Read the master Fernet key from env. If absent and we're in DEV_MODE,
    persist a random key on disk so the dashboard works locally; otherwise
    raise — production must set ``CB_KEY_STORE_SECRET`` explicitly so the
    operator owns the key material."""
    raw = os.environ.get(SECRET_ENV, "").strip()
    if raw:
        try:
            Fernet(raw.encode("utf-8"))   # validate
            return raw.encode("utf-8")
        except Exception as exc:
            raise RuntimeError(
                f"{SECRET_ENV} is set but not a valid Fernet key. "
                f"Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            ) from exc

    if os.environ.get("DEV_MODE", "").strip() == "1":
        dev_key_path = DEFAULT_DB_PATH.parent / "dev_master.key"
        dev_key_path.parent.mkdir(parents=True, exist_ok=True)
        if dev_key_path.exists():
            return dev_key_path.read_bytes().strip()
        new_key = Fernet.generate_key()
        dev_key_path.write_bytes(new_key)
        try:
            os.chmod(dev_key_path, 0o600)
        except OSError:
            pass
        log.warning(
            "DEV_MODE: generated random master key at %s — fine for local "
            "development, NEVER use in production.",
            dev_key_path,
        )
        return new_key

    raise RuntimeError(
        f"{SECRET_ENV} is required for trading endpoints. Set it (urlsafe-base64 "
        f"32-byte key) and restart, or set DEV_MODE=1 for local-only testing."
    )


def _connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_keys (
            user_id           TEXT PRIMARY KEY,
            api_key_id_enc    BLOB NOT NULL,
            private_key_enc   BLOB NOT NULL,
            mode              TEXT NOT NULL DEFAULT 'paper',
            created_at        INTEGER NOT NULL,
            updated_at        INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


@dataclass
class StoredKey:
    user_id: str
    api_key_id: str
    private_key_pem: bytes
    mode: str            # "paper" | "prod"
    created_at: int
    updated_at: int


def upsert_key(
    user_id: str,
    api_key_id: str,
    private_key_pem: str | bytes,
    mode: str = "paper",
    *,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Encrypt and store a user's API credentials. ``mode`` defaults to
    ``"paper"`` — flipping to ``"prod"`` is its own action."""
    if mode not in ("paper", "prod"):
        raise ValueError(f"mode must be 'paper' or 'prod', got {mode!r}")
    if isinstance(private_key_pem, str):
        private_key_pem = private_key_pem.encode("utf-8")
    # Light sanity check — actual RSA validation happens in kalshi_auth on first sign.
    if b"PRIVATE KEY" not in private_key_pem:
        raise ValueError("private_key_pem doesn't look like a PEM private key")

    master = _get_master_key()
    f = Fernet(master)
    enc_id = f.encrypt(api_key_id.encode("utf-8"))
    enc_pk = f.encrypt(private_key_pem)
    import time
    now = int(time.time())

    with _lock, _connect(db_path) as conn:
        conn.execute("""
            INSERT INTO user_keys (user_id, api_key_id_enc, private_key_enc, mode, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              api_key_id_enc=excluded.api_key_id_enc,
              private_key_enc=excluded.private_key_enc,
              mode=excluded.mode,
              updated_at=excluded.updated_at
        """, (user_id, enc_id, enc_pk, mode, now, now))
        conn.commit()


def get_key(user_id: str, *, db_path: Path = DEFAULT_DB_PATH) -> StoredKey | None:
    master = _get_master_key()
    f = Fernet(master)
    with _lock, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT user_id, api_key_id_enc, private_key_enc, mode, created_at, updated_at "
            "FROM user_keys WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    try:
        api_key_id = f.decrypt(row["api_key_id_enc"]).decode("utf-8")
        pem = f.decrypt(row["private_key_enc"])
    except InvalidToken as exc:
        # Almost always means the master key changed — operator rotated
        # CB_KEY_STORE_SECRET. Without the old key, ciphertexts are dead.
        raise RuntimeError(
            "Stored key cannot be decrypted with the current CB_KEY_STORE_SECRET. "
            "The user must re-enter their Kalshi credentials."
        ) from exc
    return StoredKey(
        user_id=row["user_id"],
        api_key_id=api_key_id,
        private_key_pem=pem,
        mode=row["mode"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def delete_key(user_id: str, *, db_path: Path = DEFAULT_DB_PATH) -> bool:
    with _lock, _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM user_keys WHERE user_id = ?", (user_id,))
        conn.commit()
    return cur.rowcount > 0


def set_mode(user_id: str, mode: str, *, db_path: Path = DEFAULT_DB_PATH) -> bool:
    if mode not in ("paper", "prod"):
        raise ValueError(f"mode must be 'paper' or 'prod', got {mode!r}")
    import time
    now = int(time.time())
    with _lock, _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE user_keys SET mode = ?, updated_at = ? WHERE user_id = ?",
            (mode, now, user_id),
        )
        conn.commit()
    return cur.rowcount > 0


def status(user_id: str, *, db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Return a key's metadata without decrypting the secret material — safe
    to call without the master key on hand."""
    with _lock, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT mode, created_at, updated_at FROM user_keys WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"configured": False}
    return {
        "configured": True,
        "mode": row["mode"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# --- Self-test --------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
    from cryptography.hazmat.primitives import serialization as _ser

    os.environ.setdefault("DEV_MODE", "1")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        # Make the dev master key sit in the same dir so we don't pollute /Users
        os.environ.setdefault(SECRET_ENV, Fernet.generate_key().decode())
        priv = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = priv.private_bytes(
            encoding=_ser.Encoding.PEM,
            format=_ser.PrivateFormat.PKCS8,
            encryption_algorithm=_ser.NoEncryption(),
        )
        upsert_key("user-1", "kalshi-key-abc", pem, mode="paper", db_path=db)
        retrieved = get_key("user-1", db_path=db)
        assert retrieved is not None
        assert retrieved.api_key_id == "kalshi-key-abc"
        assert retrieved.private_key_pem == pem
        assert retrieved.mode == "paper"
        print("✓ upsert + get round-trip ok")
        st = status("user-1", db_path=db)
        assert st["configured"] is True and st["mode"] == "paper"
        print("✓ status reports paper mode")
        set_mode("user-1", "prod", db_path=db)
        st2 = status("user-1", db_path=db)
        assert st2["mode"] == "prod"
        print("✓ mode toggle paper → prod ok")
        ok = delete_key("user-1", db_path=db)
        assert ok and get_key("user-1", db_path=db) is None
        print("✓ delete clears the row")
        print("all key-store tests passed")
