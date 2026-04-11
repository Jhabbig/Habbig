"""Two-factor authentication primitives — TOTP, backup codes, email OTP.

Reuses:
  - backend.markets.encryption for Fernet wrapping of TOTP secrets
  - db._hash_password for backup code + email OTP hashing (PBKDF2-HMAC-SHA256)
  - db.rate_limit_hit / db.insert_2fa_attempt / db.recent_2fa_failures for
    persistent lockout state

External deps: pyotp (for TOTP), qrcode (for QR code PNG).
"""

from __future__ import annotations

import base64
import io
import logging
import secrets

log = logging.getLogger("gateway.two_factor")

# ── Constants ─────────────────────────────────────────────────────────────────

TOTP_ISSUER = "narve.ai"
TOTP_DIGITS = 6
TOTP_PERIOD = 30

BACKUP_CODE_COUNT = 8

EMAIL_OTP_LENGTH = 6
EMAIL_OTP_TTL_SECONDS = 600  # 10 minutes

TWO_FA_LOCKOUT_LIMIT = 5
TWO_FA_LOCKOUT_WINDOW = 600  # 10 minutes
TWO_FA_LOCKOUT_DURATION = 900  # 15 minutes
TWO_FA_RESEND_WINDOW = 60  # one resend per minute


# ── TOTP (authenticator app) ─────────────────────────────────────────────────


def generate_totp_secret() -> str:
    """Return a fresh base32 TOTP secret (20 bytes, pyotp default)."""
    import pyotp
    return pyotp.random_base32()


def encrypt_totp_secret(secret: str) -> str:
    """Fernet-encrypt a base32 TOTP secret. Reuses CREDENTIALS_ENCRYPTION_KEY."""
    from backend.markets.encryption import encrypt_token
    return encrypt_token(secret)


def decrypt_totp_secret(encrypted: str) -> str:
    from backend.markets.encryption import decrypt_token
    return decrypt_token(encrypted)


def build_totp_uri(secret: str, account_label: str) -> str:
    """Build an otpauth:// URI for QR display.

    account_label is usually the user's email address.
    """
    import pyotp
    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD)
    return totp.provisioning_uri(name=account_label, issuer_name=TOTP_ISSUER)


def build_qr_data_uri(otp_uri: str) -> str:
    """Render otp_uri as a PNG, then return a base64 `data:image/png` URI."""
    import qrcode
    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(otp_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def verify_totp_code(secret: str, code: str, drift_periods: int = 1) -> bool:
    """Verify a 6-digit code against *secret*. Accepts ±*drift_periods*×30s drift."""
    import pyotp
    code = (code or "").strip().replace(" ", "")
    if not code or len(code) != TOTP_DIGITS or not code.isdigit():
        return False
    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD)
    try:
        return bool(totp.verify(code, valid_window=drift_periods))
    except Exception as e:
        log.warning("TOTP verify raised: %s", e)
        return False


# ── Backup codes ─────────────────────────────────────────────────────────────


def _format_code(raw_hex: str) -> str:
    """Split 8 hex chars as XXXX-XXXX, uppercase."""
    up = raw_hex.upper()
    return f"{up[:4]}-{up[4:8]}"


def generate_backup_codes(count: int = BACKUP_CODE_COUNT) -> list[str]:
    """Return *count* fresh human-readable backup codes (plaintext)."""
    return [_format_code(secrets.token_hex(4)) for _ in range(count)]


def hash_backup_code(plaintext: str) -> dict:
    """Hash a single backup code. Reuses db._hash_password (PBKDF2-HMAC-SHA256)."""
    import db
    h, salt = db._hash_password(plaintext)
    return {"hash": h, "salt": salt, "used_at": None}


def hash_backup_codes(plaintexts: list[str]) -> list[dict]:
    return [hash_backup_code(p) for p in plaintexts]


# ── Email one-time passwords ─────────────────────────────────────────────────


def generate_email_otp() -> str:
    """Return a zero-padded 6-digit numeric code."""
    return str(secrets.randbelow(10 ** EMAIL_OTP_LENGTH)).zfill(EMAIL_OTP_LENGTH)


def hash_email_otp(plaintext: str) -> tuple[str, str]:
    """Return (hash, salt) for storing an email OTP. Uses db._hash_password."""
    import db
    return db._hash_password(plaintext)


def verify_email_otp_code(plaintext: str, stored_hash: str, salt: str) -> bool:
    import db
    return db.verify_password(plaintext, stored_hash, salt)


# ── Rate limiting / lockout ──────────────────────────────────────────────────


def _lockout_key(user_id: int, ip: str) -> str:
    return f"2fa:{user_id}:{ip or 'unknown'}"


def is_2fa_locked(user_id: int, ip: str) -> bool:
    """True iff the (user, ip) pair has tripped the 2FA lockout recently.

    Uses the persistent `rate_limits` table, which means process restarts do
    not reset the clock. Once locked, no verify attempts are accepted for
    `TWO_FA_LOCKOUT_DURATION` seconds.
    """
    import db
    # check-only: we don't record a hit here, that's record_2fa_attempt's job
    return db.rate_limit_check(
        _lockout_key(user_id, ip),
        TWO_FA_LOCKOUT_LIMIT,
        TWO_FA_LOCKOUT_DURATION,
    )


def record_2fa_attempt(user_id: int, method: str, success: bool, ip: str) -> None:
    """Persist attempt + bump rate-limit bucket on failure.

    Successful attempts are audited but do NOT bump the bucket.
    """
    import db
    db.insert_2fa_attempt(user_id, method, success, ip)
    if not success:
        db.rate_limit_hit(
            _lockout_key(user_id, ip),
            TWO_FA_LOCKOUT_LIMIT,
            TWO_FA_LOCKOUT_DURATION,
        )


def can_resend_email_otp(user_id: int) -> bool:
    """Return True iff a fresh email OTP may be sent right now.

    Uses a dedicated persistent bucket (1 send per 60 seconds).
    """
    import db
    # rate_limit_hit records AND checks — we want to record only when actually sending
    return not db.rate_limit_check(f"2fa_send:{user_id}", 1, TWO_FA_RESEND_WINDOW)


def mark_email_otp_sent(user_id: int) -> None:
    import db
    db.rate_limit_hit(f"2fa_send:{user_id}", 1, TWO_FA_RESEND_WINDOW)
