"""Signed token helpers for embed widgets.

The token that lives in a partner's ``<iframe src=...?token=XXX>`` URL is
an HMAC-SHA256 over ``(widget_id, token_salt)`` using the environment
variable ``EMBED_SIGNING_SECRET``. Properties:

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

If ``EMBED_SIGNING_SECRET`` is unset we fall back to ``GATEWAY_SSO_SECRET``
(the gateway's existing shared secret). If neither is set we generate a
stable per-process secret and log a warning — useful for tests and dev,
unsafe for production because tokens would rotate every restart.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets


log = logging.getLogger("embed_tokens")

_PROCESS_FALLBACK: bytes | None = None


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


def sign(widget_id: str, token_salt: str) -> str:
    """Return a URL-safe token tying a widget_id to its current salt."""
    msg = f"{widget_id}:{token_salt}".encode("utf-8")
    mac = hmac.new(_get_secret(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def verify(widget_id: str, token_salt: str, token: str) -> bool:
    """Constant-time compare a provided token against the expected signature."""
    expected = sign(widget_id, token_salt)
    return hmac.compare_digest(expected, token or "")


def new_salt() -> str:
    """Fresh random salt; rotate this to invalidate existing tokens."""
    return secrets.token_urlsafe(24)


def new_widget_id() -> str:
    """Public URL-safe identifier for a widget (used in ``/embed/{widget_id}``)."""
    return secrets.token_urlsafe(12)
