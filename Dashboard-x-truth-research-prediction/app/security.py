from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


def _get_or_create_encryption_key() -> str:
    """Get encryption key from env, or generate and persist to a local file."""
    key = os.environ.get("ENCRYPTION_KEY")
    if key:
        return key
    key_file = Path(__file__).parent.parent / ".encryption_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = Fernet.generate_key().decode()
    try:
        key_file.write_text(key)
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


_ENCRYPTION_KEY = _get_or_create_encryption_key()
_fernet = Fernet(_ENCRYPTION_KEY if isinstance(_ENCRYPTION_KEY, bytes) else _ENCRYPTION_KEY.encode())


def encrypt_field(value: str) -> str:
    if not value:
        return ""
    return _fernet.encrypt(value.encode()).decode()


def decrypt_field(value: str) -> str:
    """Decrypt a Fernet-encrypted string. Returns the original value if decryption
    fails (legacy plaintext or rotated key)."""
    if not value:
        return ""
    try:
        return _fernet.decrypt(value.encode()).decode()
    except Exception:
        logger.warning("Failed to decrypt field — possible key rotation or legacy plaintext")
        return value
