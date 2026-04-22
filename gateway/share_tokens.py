"""Signed, expiring tokens for shareable artifacts.

Every share URL — /s/m/{token}, /s/s/{token}, /s/p/{token} — embeds a
short opaque string that decodes to (kind, row_id, sharer_user_id,
expires_at). The string is HMAC-signed with the gateway's cookie
secret, so a viewer can't forge a link or extend an expiry without
the key.

Format: ``{kind}.{payload_b64url}.{sig_b64url}``

  * ``kind``      — one of ``m`` | ``s`` | ``p`` (market/source/pred).
                    Kept in the plaintext so routes can dispatch
                    without decoding the payload first.
  * ``payload``   — JSON blob, b64url-encoded, no padding. Fields:
                      r  row id of the shared_* row
                      u  sharer user id
                      e  expiry unix timestamp
                      n  nonce (32 bits) to prevent replay collisions
                    The nonce + DB-side uniqueness on the token column
                    means a re-generation of a token returns a new
                    string even for the same (r, u, e) triple.
  * ``sig``       — HMAC-SHA256 over ``{kind}.{payload_b64url}``,
                    b64url without padding.

Rotation: the secret is read from ``SHARE_TOKEN_SECRET`` env var,
falling back to ``GATEWAY_COOKIE_SECRET`` so a fresh deploy without
the new var still works. Both are loaded lazily so tests can set
either at import time.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Optional


ShareKind = Literal["m", "s", "p"]
_VALID_KINDS: tuple[ShareKind, ...] = ("m", "s", "p")

# 7-day default expiry matches the spec. Callers can override per-call
# if a specific share needs a shorter lifetime (e.g. a preview link).
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class DecodedToken:
    kind: ShareKind
    row_id: int
    sharer_user_id: int
    expires_at: int


class InvalidToken(ValueError):
    """Decoding failed — either the signature didn't verify, the kind
    was unknown, the payload didn't parse, or the token expired. The
    route layer treats all four identically (404) so we don't leak
    which class of failure hit."""


def _secret() -> bytes:
    """Lazy-load the HMAC secret.

    Prefer a feature-specific secret (rotatable without touching
    sessions); fall back to the shared cookie secret so fresh deploys
    work out of the box. Refuse to run with a dev-only value in
    production — caller-side checks (server.py) already enforce the
    cookie-secret presence in prod, so if we got that far the fallback
    is safe."""
    for env in ("SHARE_TOKEN_SECRET", "GATEWAY_COOKIE_SECRET"):
        val = os.environ.get(env, "").strip()
        if val:
            return val.encode("utf-8")
    # Dev fallback — unit tests that don't set a secret should still run.
    return b"narve-share-dev-secret"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    # base64.urlsafe_b64decode requires padding; add it back.
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload_b64: str, kind: ShareKind) -> str:
    mac = hmac.new(_secret(), f"{kind}.{payload_b64}".encode("ascii"), sha256)
    return _b64url_encode(mac.digest())


def encode(
    *,
    kind: ShareKind,
    row_id: int,
    sharer_user_id: int,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: Optional[int] = None,
) -> tuple[str, int]:
    """Mint a signed token. Returns ``(token_string, expires_at_unix)``
    so callers can persist the expiry without recomputing it.

    ``now`` is injectable for tests; production callers pass nothing."""
    if kind not in _VALID_KINDS:
        raise ValueError(f"invalid kind: {kind!r}")
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
    t = int(now if now is not None else time.time())
    expires_at = t + ttl_seconds
    payload = {
        "r": int(row_id),
        "u": int(sharer_user_id),
        "e": expires_at,
        # Nonce ensures two tokens minted in the same second for the
        # same (r, u) never collide. 32 bits of entropy is plenty — the
        # DB's UNIQUE(token) catches any astronomical collision anyway.
        "n": secrets.randbits(32),
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("ascii"))
    sig = _sign(payload_b64, kind)
    return f"{kind}.{payload_b64}.{sig}", expires_at


def decode(token: str, *, now: Optional[int] = None) -> DecodedToken:
    """Verify + parse a token. Raises ``InvalidToken`` on any failure.

    The single exception type is intentional — distinguishing "expired"
    from "tampered" from "malformed" lets an attacker probe the system
    for valid-but-expired tokens. Callers translate every failure into
    a generic 404."""
    if not isinstance(token, str) or not token:
        raise InvalidToken("empty token")

    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidToken("malformed: expected 3 dot-separated segments")
    kind_raw, payload_b64, sig = parts
    if kind_raw not in _VALID_KINDS:
        raise InvalidToken(f"unknown kind {kind_raw!r}")
    kind: ShareKind = kind_raw  # type: ignore[assignment]

    # Constant-time signature comparison — a direct == check would
    # leak the first-mismatching-byte timing across attackers.
    expected_sig = _sign(payload_b64, kind)
    if not hmac.compare_digest(sig, expected_sig):
        raise InvalidToken("signature mismatch")

    try:
        raw = _b64url_decode(payload_b64)
        data = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise InvalidToken("payload not JSON")

    try:
        row_id = int(data["r"])
        sharer = int(data["u"])
        expires_at = int(data["e"])
    except (KeyError, TypeError, ValueError):
        raise InvalidToken("payload missing required fields")

    t = int(now if now is not None else time.time())
    if expires_at < t:
        raise InvalidToken("expired")

    return DecodedToken(
        kind=kind, row_id=row_id, sharer_user_id=sharer, expires_at=expires_at,
    )


def peek_kind(token: str) -> Optional[ShareKind]:
    """Return the kind prefix WITHOUT verifying the signature. Useful
    for a dispatcher that routes to the right handler before paying
    the HMAC cost. The handler must still call ``decode`` to validate."""
    if not isinstance(token, str) or "." not in token:
        return None
    prefix = token.split(".", 1)[0]
    return prefix if prefix in _VALID_KINDS else None  # type: ignore[return-value]
