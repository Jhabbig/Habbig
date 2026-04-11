"""Fernet symmetric encryption for stored Kalshi tokens."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("gateway.encryption")

_fernet = None


def _get_fernet():
    """Lazy-init Fernet using CREDENTIALS_ENCRYPTION_KEY env var."""
    global _fernet
    if _fernet is not None:
        return _fernet

    key = os.environ.get("CREDENTIALS_ENCRYPTION_KEY", "")
    if not key:
        log.warning(
            "CREDENTIALS_ENCRYPTION_KEY not set — credential encryption disabled. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
        return None

    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        return _fernet
    except ImportError:
        log.warning("cryptography package not installed — credential encryption disabled")
        return None
    except Exception as e:
        log.error("Failed to init Fernet: %s", e)
        return None


def encrypt_token(token: str) -> str:
    """Encrypt a token string. Returns encrypted base64 string.

    In production, raises RuntimeError if encryption is unavailable.
    In dev, falls back to plaintext with warning.
    """
    f = _get_fernet()
    if f is None:
        if os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes", "on"):
            raise RuntimeError("CREDENTIALS_ENCRYPTION_KEY is required in production for storing Kalshi tokens")
        log.warning("Storing token without encryption — set CREDENTIALS_ENCRYPTION_KEY")
        return token
    return f.encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Decrypt an encrypted token string.

    Falls back to returning the input as-is if decryption fails
    (handles migration from unencrypted to encrypted storage).
    """
    f = _get_fernet()
    if f is None:
        return encrypted
    try:
        return f.decrypt(encrypted.encode()).decode()
    except Exception:
        # May be a plaintext token from before encryption was enabled
        return encrypted
