from __future__ import annotations
"""API key generation and verification.

Keys are returned plaintext exactly once (at creation) and stored as
SHA-256 hashes. The first 8 chars after the prefix double as a key_prefix
for display purposes (`mte_live_abc12345…`).

Tier → rate limit table is the only place per-tier RPM caps are defined.
"""

import hashlib
import os
import secrets

KEY_PREFIX = "mte_live_"  # "MidtermEdge live"

TIER_RATE_LIMITS_RPM = {
    "free": 60,         # public API, anonymous-like
    "premium": 600,     # paying solo users
    "enterprise": 6000, # campaigns, hedge funds, journos
    "admin": 0,         # unlimited
}


def generate() -> tuple[str, str, str]:
    """Generate a new API key.

    Returns ``(plaintext_key, key_prefix, key_hash)``. The plaintext is
    shown to the user once; only the hash is stored.
    """
    secret = secrets.token_urlsafe(32)
    plaintext = f"{KEY_PREFIX}{secret}"
    key_prefix = plaintext[: len(KEY_PREFIX) + 8]  # "mte_live_abc12345"
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return plaintext, key_prefix, key_hash


def hash_key(plaintext: str) -> str:
    """Hash a plaintext key for lookup."""
    return hashlib.sha256(plaintext.strip().encode("utf-8")).hexdigest()


def rate_limit_for(tier: str) -> int:
    return TIER_RATE_LIMITS_RPM.get(tier, 60)
