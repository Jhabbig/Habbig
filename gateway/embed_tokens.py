"""Signed token helpers for embed widgets.

Tokens are a compact ``base64url(payload_json).base64url(hmac)`` pair
signed with ``EMBED_SIGNING_SECRET`` (falling back to
``GATEWAY_SSO_SECRET``). The payload carries:

    * ``w``   — widget_id
    * ``s``   — token_salt (so a rotate invalidates old tokens even if
                the attacker preserves the payload)
    * ``iat`` — issued-at, unix seconds
    * ``exp`` — expiry, unix seconds (default 90 days; user can rotate
                via ``api_rotate_embed_token``)

Properties:

    * **Rotatable without new widget URLs**: bump ``token_salt`` in the
      row and every old token's HMAC stops matching.
    * **DB dump alone doesn't forge tokens**: the secret lives only in
      the process environment. A leaked ``embed_widgets`` table is not
      sufficient to sign new tokens.
    * **Stateless verification**: the crypto check needs no DB round
      trip; the DB is still consulted afterwards for ``is_active`` and
      the owner's subscription status.
    * **Constant-time compare** (``hmac.compare_digest``) to avoid
      timing side-channels.
    * **Bounded lifetime (H16)**: expired tokens are rejected even if
      the HMAC is valid and the salt has not rotated.

If ``EMBED_SIGNING_SECRET`` is unset we fall back to ``GATEWAY_SSO_SECRET``
(the gateway's existing shared secret). If neither is set we generate a
stable per-process secret and log a warning — useful for tests and dev,
unsafe for production because tokens would rotate every restart.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Optional


log = logging.getLogger("embed_tokens")

_PROCESS_FALLBACK: bytes | None = None

# Default embed-token lifetime: 90 days. Long-lived by design — partners
# paste the iframe once and forget. Users can forcibly invalidate by
# rotating the widget's token_salt.
DEFAULT_EMBED_TOKEN_TTL = 90 * 24 * 3600


def _get_secret() -> bytes:
    """Return the signing secret as bytes. See module docstring for fallbacks."""
    global _PROCESS_FALLBACK
    env_secret = os.environ.get("EMBED_SIGNING_SECRET") or os.environ.get("GATEWAY_SSO_SECRET")
    if env_secret:
        return env_secret.encode("utf-8")
    if _PROCESS_FALLBACK is None:
        log.warning(
            "EMBED_SIGNING_SECRET and GATEWAY_SSO_SECRET are both unset — "
            "using an in-memory fallback. Set EMBED_SIGNING_SECRET in "
            "production so tokens survive restart."
        )
        _PROCESS_FALLBACK = secrets.token_urlsafe(32).encode("utf-8")
    return _PROCESS_FALLBACK


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def sign(widget_id: str, token_salt: str, ttl_seconds: int = DEFAULT_EMBED_TOKEN_TTL) -> str:
    """Return a URL-safe ``payload.sig`` token tying widget_id + salt + exp."""
    now = int(time.time())
    payload = {
        "w": widget_id,
        "s": token_salt,
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    mac = hmac.new(_get_secret(), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(mac)}"


def _verify_legacy(widget_id: str, token_salt: str, token: str) -> bool:
    """Legacy bare-HMAC format (no iat/exp). Kept only so already-issued
    tokens do not simultaneously 401 the moment this code deploys;
    callers still enforce expiry on the new format and a rotate-token
    flips everyone forward."""
    msg = f"{widget_id}:{token_salt}".encode("utf-8")
    expected = _b64url_encode(hmac.new(_get_secret(), msg, hashlib.sha256).digest())
    return hmac.compare_digest(expected, token or "")


def verify(widget_id: str, token_salt: str, token: str, *, now: Optional[int] = None) -> bool:
    """Return True iff ``token`` is a valid, non-expired embed token for
    ``(widget_id, token_salt)``. Constant-time on the signature check."""
    if not token:
        return False
    # New payload.sig format
    if "." in token:
        try:
            payload_b64, sig_b64 = token.split(".", 1)
            payload_bytes = _b64url_decode(payload_b64)
            sig = _b64url_decode(sig_b64)
        except Exception:
            return False
        expected_sig = hmac.new(_get_secret(), payload_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_sig, sig):
            return False
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception:
            return False
        if payload.get("w") != widget_id:
            return False
        if payload.get("s") != token_salt:
            return False
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        current = int(now if now is not None else time.time())
        if current >= int(exp):
            return False
        return True
    # Legacy format: signature-only, no exp claim. Accept for now to
    # avoid breaking existing partner iframes; these get re-minted the
    # next time the widget is rotated.
    return _verify_legacy(widget_id, token_salt, token)


def new_salt() -> str:
    """Fresh random salt; rotate this to invalidate existing tokens."""
    return secrets.token_urlsafe(24)


def new_widget_id() -> str:
    """Public URL-safe identifier for a widget (used in ``/embed/{widget_id}``)."""
    return secrets.token_urlsafe(12)
