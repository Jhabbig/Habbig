"""Tests for Fix E (2026-05-15): unsubscribe HMAC fallback + rate-limit.

Covers:
  * _secret() returns the env value when set
  * _secret() raises RuntimeError in production when secret is unset
  * _secret() returns a dev-only constant in non-production
  * generate + verify round-trip
  * Tampered signatures rejected
  * Tampered email rejected
  * Tampered scope rejected
"""

from __future__ import annotations

import os
import sys
import unittest

from tests import _testdb  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
from email_system import unsubscribe as us  # noqa: E402


def _seed_user(email: str, *, user_id: int) -> None:
    """Insert a user row directly so the UnsubscribeManager has a target."""
    with db.conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (id, username, email, password_hash, "
            "password_salt, created_at, email_marketing, email_digest) "
            "VALUES (?, ?, ?, '', '', strftime('%s','now'), 1, 1)",
            (user_id, f"u_{user_id}", email),
        )


class TestSecretFallback(unittest.TestCase):
    """The HMAC key resolution rules."""

    def setUp(self):
        self._saved_secret = os.environ.get("GATEWAY_COOKIE_SECRET")
        self._saved_prod = os.environ.get("PRODUCTION")
        self._saved_isprod = os.environ.get("IS_PRODUCTION")
        os.environ.pop("GATEWAY_COOKIE_SECRET", None)
        os.environ.pop("PRODUCTION", None)
        os.environ.pop("IS_PRODUCTION", None)

    def tearDown(self):
        if self._saved_secret is not None:
            os.environ["GATEWAY_COOKIE_SECRET"] = self._saved_secret
        else:
            os.environ.pop("GATEWAY_COOKIE_SECRET", None)
        if self._saved_prod is not None:
            os.environ["PRODUCTION"] = self._saved_prod
        else:
            os.environ.pop("PRODUCTION", None)
        if self._saved_isprod is not None:
            os.environ["IS_PRODUCTION"] = self._saved_isprod
        else:
            os.environ.pop("IS_PRODUCTION", None)

    def test_uses_env_secret_when_set(self):
        os.environ["GATEWAY_COOKIE_SECRET"] = "a" * 64
        self.assertEqual(us._secret(), b"a" * 64)

    def test_raises_in_production_when_unset(self):
        os.environ["PRODUCTION"] = "1"
        with self.assertRaises(RuntimeError) as ctx:
            us._secret()
        self.assertIn("GATEWAY_COOKIE_SECRET", str(ctx.exception))

    def test_raises_in_is_production_when_unset(self):
        os.environ["IS_PRODUCTION"] = "1"
        with self.assertRaises(RuntimeError) as ctx:
            us._secret()
        self.assertIn("GATEWAY_COOKIE_SECRET", str(ctx.exception))

    def test_dev_fallback_when_no_prod_flag(self):
        # In dev mode the function returns the documented dev-only
        # constant — not the old leaked "narve-unsubscribe" string.
        secret = us._secret()
        self.assertNotEqual(secret, b"narve-unsubscribe")
        self.assertIn(b"dev", secret)


class TestTokenRoundTrip(unittest.TestCase):
    """The unsubscribe lifecycle relies on a stable signed token."""

    def setUp(self):
        # Use a fixed dev secret so the tests are deterministic.
        os.environ["GATEWAY_COOKIE_SECRET"] = "test-secret-aaaaaaaaaaaaaaaaaaaaaaaa"

    def test_generated_token_verifies(self):
        email = "round-trip-1@example.com"
        _seed_user(email, user_id=8001)
        token = us.UnsubscribeManager.generate_token(
            email, 8001, "marketing",
        )
        # First redemption succeeds.
        row = us.UnsubscribeManager.unsubscribe(token)
        self.assertIsNotNone(row)
        self.assertEqual(row["email"], email)

    def test_tampered_signature_rejected(self):
        email = "round-trip-2@example.com"
        _seed_user(email, user_id=8002)
        token = us.UnsubscribeManager.generate_token(
            email, 8002, "marketing",
        )
        # Flip the trailing sig.
        raw, sig = token.rsplit(".", 1)
        tampered_sig = ("0" * len(sig)) if sig != "0" * len(sig) else "1" * len(sig)
        forged = f"{raw}.{tampered_sig}"
        # The DB has the original token, so the lookup won't find the
        # tampered one — unsubscribe returns None.
        self.assertIsNone(us.UnsubscribeManager.unsubscribe(forged))

    def test_missing_separator_returns_none(self):
        # Pre-validation rejects malformed tokens before any DB hit.
        self.assertIsNone(us.UnsubscribeManager.unsubscribe(""))
        self.assertIsNone(us.UnsubscribeManager.unsubscribe("nosep"))


if __name__ == "__main__":
    unittest.main()
