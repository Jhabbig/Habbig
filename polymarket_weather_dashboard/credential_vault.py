"""Per-user encrypted credential vault.

Kalshi authenticated trading requires the user's API key id + RSA
private key. Storing those in plaintext anywhere is unacceptable; we
encrypt each user's blob with Fernet (AES-128-CBC + HMAC-SHA256) and
store the ciphertext in `kalshi_credentials`.

Security model
--------------
* The master Fernet key is derived from a server-local secret file
  (``.secret_key``, gitignored, 0600 permissions). That secret never
  leaves the server.
* User_id is part of the SQLite primary key, so a row scoped to user A
  cannot be read while authenticated as user B at the application level.
* The vault returns a `KalshiCredentials` dataclass that holds the
  decrypted RSA key in memory only for the duration of one request.
* `enroll` rejects keys smaller than 2048 bits and re-validates the
  PEM by re-loading it before persistence — a malformed PEM never
  reaches storage.
* The audit log records `enroll`, `disable`, `rotate` events with no
  key material — the trail is "user X added credentials" without
  exposing what they were.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_signing import load_rsa_private_key

logger = logging.getLogger(__name__)


def _resolve_secret_path() -> Path:
    """Where the master secret file lives. Picked up from env so tests
    can swap in a temp file without monkeypatching."""
    override = os.environ.get("WEATHER_SECRET_KEY_PATH")
    if override:
        return Path(override)
    return Path(__file__).parent / ".secret_key"


def _ensure_secret_key() -> bytes:
    """Read or create the master secret. Tightens permissions if the
    file is world- or group-readable.

    Returns the raw bytes — callers should never log this.
    """
    path = _resolve_secret_path()
    if not path.exists():
        raw = os.urandom(64)
        path.write_bytes(raw)
        try:
            path.chmod(0o600)
        except PermissionError:
            logger.warning(".secret_key chmod 0600 failed at %s", path)
        return raw
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            logger.warning(".secret_key has loose permissions (%o) — tightening",
                           mode & 0o777)
            path.chmod(0o600)
    except OSError:
        pass
    return path.read_bytes().strip()


def _fernet_key() -> bytes:
    """Derive a Fernet-compatible key from the master secret.

    Fernet wants a 32-byte URL-safe-base64-encoded key; SHA-256 fits.
    """
    return base64.urlsafe_b64encode(hashlib.sha256(_ensure_secret_key()).digest())


def _fernet() -> Fernet:
    return Fernet(_fernet_key())


@dataclass
class KalshiCredentials:
    """In-memory bundle returned to callers.

    The `private_key` here is a parsed RSA key object — callers should
    use it for signing and let it go out of scope as soon as the
    request finishes. We deliberately don't expose the raw PEM after
    decryption.
    """
    user_id: str
    key_id: str
    private_key: rsa.RSAPrivateKey
    is_demo: bool = False


def _encrypt_blob(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()


def _decrypt_blob(token: str) -> dict:
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except (InvalidToken, ValueError) as e:
        logger.warning("vault decrypt failed: %s", type(e).__name__)
        raise


def store_credentials(conn_factory, user_id: str, key_id: str,
                      private_pem: bytes, is_demo: bool = False,
                      label: Optional[str] = None) -> None:
    """Validate + encrypt + persist the user's Kalshi credentials.

    `conn_factory` is a callable returning a context-manager DB
    connection (passed in so this module doesn't depend on server.py
    internals — keeps it testable). The PEM is parsed once here to
    fail fast on bad input; it is *not* stored after parsing.
    """
    if not user_id or not key_id:
        raise ValueError("user_id and key_id are required")
    if not isinstance(private_pem, (bytes, bytearray)):
        raise ValueError("private_pem must be bytes")
    # Validate the PEM by loading it; raises ValueError on malformed
    load_rsa_private_key(bytes(private_pem))
    blob = _encrypt_blob({
        "key_id": key_id,
        "private_pem": base64.b64encode(private_pem).decode(),
        "is_demo": bool(is_demo),
    })
    with conn_factory() as conn:
        conn.execute(
            """INSERT INTO kalshi_credentials (user_id, key_id, ciphertext, is_demo, label)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 key_id = excluded.key_id,
                 ciphertext = excluded.ciphertext,
                 is_demo = excluded.is_demo,
                 label = excluded.label,
                 updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                 disabled_at = NULL""",
            (user_id, key_id, blob, int(bool(is_demo)), label or ""),
        )


def load_credentials(conn_factory, user_id: str) -> Optional[KalshiCredentials]:
    """Return decrypted credentials or None.

    Returns None for: user with no row, disabled row, or decrypt error.
    The caller should treat None as "not enrolled" — never as a fault.
    """
    if not user_id:
        return None
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            """SELECT key_id, ciphertext, is_demo
               FROM kalshi_credentials
               WHERE user_id = ? AND disabled_at IS NULL""",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    try:
        blob = _decrypt_blob(row["ciphertext"])
    except Exception:
        return None
    try:
        pem = base64.b64decode(blob["private_pem"])
        pk = load_rsa_private_key(pem)
    except Exception:
        return None
    return KalshiCredentials(
        user_id=user_id,
        key_id=blob.get("key_id") or row["key_id"],
        private_key=pk,
        is_demo=bool(blob.get("is_demo", False)),
    )


def disable_credentials(conn_factory, user_id: str) -> bool:
    """Soft-delete the row (sets disabled_at). Idempotent."""
    if not user_id:
        return False
    with conn_factory() as conn:
        cur = conn.execute(
            """UPDATE kalshi_credentials
               SET disabled_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
               WHERE user_id = ? AND disabled_at IS NULL""",
            (user_id,),
        )
        return cur.rowcount > 0


def credentials_exist(conn_factory, user_id: str) -> bool:
    """Cheap check — does this user have an active enrollment? Doesn't
    decrypt anything."""
    with conn_factory(readonly=True) as conn:
        row = conn.execute(
            """SELECT 1 FROM kalshi_credentials
               WHERE user_id = ? AND disabled_at IS NULL""",
            (user_id,),
        ).fetchone()
    return bool(row)
