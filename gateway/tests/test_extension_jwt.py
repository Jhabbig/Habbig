"""Tests for the Chrome-extension JWT hardening (audit_extension_auth.md HIGH × 3).

The audit flagged three HIGH-severity gaps in ``gateway/extension_routes.py``:

  1. EXTENSION_JWT_SECRET silently fell back to GATEWAY_COOKIE_SECRET
     and ultimately to the repo-committed literal "narve-extension-dev".
     In production this could sign 7-day JWTs with a public string.
     Fix: ``_jwt_secret()`` raises RuntimeError when PRODUCTION=1 and
     no secret is set; server.py refuses to start under the same
     condition.

  2. No revocation path. Once issued, a JWT was valid for the full
     7 days regardless of password resets or admin force-logout.
     Fix: ``_verify_jwt()`` rejects any token whose ``iat`` is older
     than ``users.jwt_invalidated_before`` (the existing column that
     password-reset already bumps; server.py:5336).

  3. No ``aud`` claim. A JWT could be replayed against any extension
     build (canary vs stable) and the gateway had no way to tell.
     Fix: ``_sign_jwt()`` bakes ``aud="ext:{NARVE_EXTENSION_ID}"``,
     and ``_verify_jwt()`` rejects tokens with a mismatched aud.

These tests pin all four scenarios called out in the fix plan.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import os
import sys
import time
import unittest
from unittest import mock

from tests import _testdb  # noqa: F401 — in-memory DB + migrations
import db  # noqa: E402

# Ensure extension_routes uses the test in-memory DB connection. The
# module imports ``db`` lazily inside _verify_jwt, so we just have to
# clear any prior cached module to pick up _testdb's monkey-patched
# db.conn / db.get_user_by_id (db.get_user_by_id is unchanged, but it
# delegates through db.conn which IS patched).
if "extension_routes" in sys.modules:
    importlib.reload(sys.modules["extension_routes"])
import extension_routes  # noqa: E402


# A 32+ char dev secret keeps the secret-loader happy when PRODUCTION
# is unset; we still set EXTENSION_JWT_SECRET so verify uses the same
# key both at sign-time and verify-time even if the test reorders.
_TEST_SECRET = "test-ext-secret-" + "x" * 32


def _setenv(**kwargs):
    """Context manager: temporarily mutate os.environ."""
    return mock.patch.dict(os.environ, kwargs, clear=False)


def _delenv(*keys):
    """Context manager: remove specific keys from os.environ."""
    new = {k: v for k, v in os.environ.items() if k not in keys}
    return mock.patch.dict(os.environ, new, clear=True)


class TestSecretStartupGuard(unittest.TestCase):
    """Audit fix §1 — EXTENSION_JWT_SECRET missing in prod → refuse.

    The audit specifies the guard belongs both at boot
    (``server.py:_lifespan``) and inside ``_jwt_secret()`` so every
    sign/verify call also fails closed. We exercise the runtime guard
    here — it's the load-bearing check; the boot guard is the same
    pattern as the existing GATEWAY_COOKIE_SECRET check directly above
    it (server.py:394) and uses identical logic.
    """

    def test_missing_secret_in_production_raises(self):
        with _setenv(PRODUCTION="1"), _delenv("EXTENSION_JWT_SECRET"):
            with self.assertRaises(RuntimeError) as ctx:
                extension_routes._jwt_secret()
            self.assertIn("EXTENSION_JWT_SECRET", str(ctx.exception))
            self.assertIn("production", str(ctx.exception).lower())

    def test_missing_secret_in_dev_uses_deterministic_fallback(self):
        """Dev / test path must still work without the env var so the
        suite doesn't need to inject one into every CI run."""
        with _setenv(PRODUCTION=""), _delenv("EXTENSION_JWT_SECRET"):
            secret = extension_routes._jwt_secret()
            self.assertIsInstance(secret, bytes)
            self.assertGreater(len(secret), 0)
            # And critically: it is NOT the GATEWAY_COOKIE_SECRET value.
            # The previous code coupled the two; the fix decouples them.
            cookie = os.environ.get("GATEWAY_COOKIE_SECRET", "anything")
            if cookie:
                self.assertNotEqual(secret, cookie.encode())

    def test_set_secret_is_used_verbatim(self):
        with _setenv(EXTENSION_JWT_SECRET=_TEST_SECRET):
            self.assertEqual(
                extension_routes._jwt_secret(), _TEST_SECRET.encode()
            )

    def test_server_startup_guard_block_is_present(self):
        """The audit also requires server.py to refuse to boot when
        EXTENSION_JWT_SECRET is missing in prod. We don't spin the
        whole lifespan up — too expensive for a unit test — but we
        assert the guard block exists in source and mirrors the
        existing GATEWAY_COOKIE_SECRET pattern at server.py:394.
        """
        import pathlib
        src = pathlib.Path(extension_routes.__file__).parent / "server.py"
        text = src.read_text()
        self.assertIn(
            "EXTENSION_JWT_SECRET must be set in production",
            text,
            "server.py must raise RuntimeError when EXTENSION_JWT_SECRET "
            "is unset under PRODUCTION=1 (audit_extension_auth.md §1)",
        )
        # And the length-guard mirrors the cookie-secret length check.
        self.assertIn(
            "EXTENSION_JWT_SECRET must be at least 32 characters",
            text,
            "server.py must enforce a minimum length on EXTENSION_JWT_SECRET",
        )


class TestAudienceClaim(unittest.TestCase):
    """Audit fix §3 — JWT with wrong ``aud`` → 401."""

    def setUp(self):
        self.uid = db.create_user(
            f"audtest-{int(time.time()*1000)}@test.com",
            "InitialPass123!",
            username=f"audtest{int(time.time()*1000) % 10**8}",
        )
        # Pin sign-time + verify-time secret/aud so the only thing we
        # vary in each test is the aud claim itself.
        self._env = _setenv(
            EXTENSION_JWT_SECRET=_TEST_SECRET,
            NARVE_EXTENSION_ID="cspjbktest1234",
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_valid_aud_token_verifies(self):
        token = extension_routes._sign_jwt(self.uid)["token"]
        self.assertEqual(extension_routes._verify_jwt(token), self.uid)

    def test_wrong_aud_rejected(self):
        # Sign with the configured aud, then re-write the payload to
        # pretend the token was issued for a different extension build.
        # This is exactly the leaked-token-replayed-against-canary
        # scenario the audit calls out.
        token = extension_routes._sign_jwt(self.uid)["token"]
        header_b64, payload_b64, _sig = token.split(".")
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        )
        payload["aud"] = "ext:someotherextension"
        new_payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).rstrip(b"=").decode()
        signing_input = f"{header_b64}.{new_payload_b64}".encode()
        sig = hmac.new(
            extension_routes._jwt_secret(), signing_input, hashlib.sha256
        ).digest()
        forged = (
            f"{header_b64}.{new_payload_b64}."
            + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        )
        self.assertIsNone(extension_routes._verify_jwt(forged))

    def test_missing_aud_rejected(self):
        token = extension_routes._sign_jwt(self.uid)["token"]
        header_b64, payload_b64, _sig = token.split(".")
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        )
        payload.pop("aud", None)
        new_payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        ).rstrip(b"=").decode()
        signing_input = f"{header_b64}.{new_payload_b64}".encode()
        sig = hmac.new(
            extension_routes._jwt_secret(), signing_input, hashlib.sha256
        ).digest()
        no_aud = (
            f"{header_b64}.{new_payload_b64}."
            + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        )
        self.assertIsNone(extension_routes._verify_jwt(no_aud))

    def test_aud_changes_when_extension_id_changes(self):
        """Tokens issued for one extension build do NOT verify against
        a server whose NARVE_EXTENSION_ID has been rotated. This is the
        leaked-canary-token-replayed-against-stable defence."""
        token = extension_routes._sign_jwt(self.uid)["token"]
        # Confirm it currently verifies.
        self.assertEqual(extension_routes._verify_jwt(token), self.uid)
        # Now rotate NARVE_EXTENSION_ID — verify must reject.
        with _setenv(NARVE_EXTENSION_ID="totallydifferentid"):
            self.assertIsNone(extension_routes._verify_jwt(token))


class TestRevocationViaJwtInvalidatedBefore(unittest.TestCase):
    """Audit fix §2 — JWT with iat before users.jwt_invalidated_before → 401.

    This is the missing revocation path. The column is the same one the
    password-reset flow already bumps (server.py:5336); the fix is to
    have ``_verify_jwt`` consult it on every API call.
    """

    def setUp(self):
        self.uid = db.create_user(
            f"revoketest-{int(time.time()*1000)}@test.com",
            "InitialPass123!",
            username=f"revoke{int(time.time()*1000) % 10**8}",
        )
        self._env = _setenv(
            EXTENSION_JWT_SECRET=_TEST_SECRET,
            NARVE_EXTENSION_ID="cspjbktest1234",
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_fresh_token_verifies_when_no_invalidation(self):
        """Baseline: a freshly signed token with the column NULL passes."""
        with db.conn() as c:
            c.execute(
                "UPDATE users SET jwt_invalidated_before = NULL WHERE id = ?",
                (self.uid,),
            )
        token = extension_routes._sign_jwt(self.uid)["token"]
        self.assertEqual(extension_routes._verify_jwt(token), self.uid)

    def test_iat_before_jwt_invalidated_before_rejected(self):
        """Sign a token, then bump the column to the future. The token's
        iat is now before the cutoff and must be rejected. This is the
        admin-force-logout scenario."""
        token = extension_routes._sign_jwt(self.uid)["token"]
        # Pre-condition: token currently verifies.
        self.assertEqual(extension_routes._verify_jwt(token), self.uid)
        # Bump the cutoff to one second past the token's iat.
        future = int(time.time()) + 2
        with db.conn() as c:
            c.execute(
                "UPDATE users SET jwt_invalidated_before = ? WHERE id = ?",
                (future, self.uid),
            )
        self.assertIsNone(extension_routes._verify_jwt(token))

    def test_password_change_invalidates_outstanding_jwts(self):
        """End-to-end: sign a JWT, simulate the password-change update
        that server.py:5336 performs, verify the token is rejected.

        This mirrors the production reset codepath exactly — the only
        differences are (a) we skip the email + token-claim choreography
        and (b) we don't hash a real new password (irrelevant to the
        column we care about).
        """
        token = extension_routes._sign_jwt(self.uid)["token"]
        self.assertEqual(extension_routes._verify_jwt(token), self.uid)

        # Same SQL the password-reset path emits (server.py:5336).
        now = int(time.time()) + 1  # +1 second so iat < now strictly.
        with db.conn() as c:
            c.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, "
                "jwt_invalidated_before = ? WHERE id = ?",
                (b"fake-hash", b"fake-salt", now, self.uid),
            )

        self.assertIsNone(
            extension_routes._verify_jwt(token),
            "extension JWT must be rejected once "
            "users.jwt_invalidated_before is bumped past its iat",
        )

    def test_token_issued_after_invalidation_still_verifies(self):
        """A NEW token minted *after* the column was bumped must still
        work — only previously-issued tokens should be killed. Otherwise
        the user could never re-link the extension after a password
        change."""
        now = int(time.time())
        with db.conn() as c:
            c.execute(
                "UPDATE users SET jwt_invalidated_before = ? WHERE id = ?",
                (now, self.uid),
            )
        # Mint a token at least one second later so iat > cutoff.
        time.sleep(1.1)
        token = extension_routes._sign_jwt(self.uid)["token"]
        self.assertEqual(extension_routes._verify_jwt(token), self.uid)

    def test_unknown_user_id_rejected(self):
        """Defence-in-depth: a token forged with sub=<deleted user> via
        the legitimate secret would otherwise verify until exp. We
        reject when the row no longer exists."""
        token = extension_routes._sign_jwt(self.uid)["token"]
        with db.conn() as c:
            c.execute("DELETE FROM users WHERE id = ?", (self.uid,))
        self.assertIsNone(extension_routes._verify_jwt(token))


class TestVerifySanity(unittest.TestCase):
    """Sanity checks that the existing happy-path still works after the
    audit fixes — guards against the fix accidentally breaking the
    common case."""

    def setUp(self):
        self._env = _setenv(
            EXTENSION_JWT_SECRET=_TEST_SECRET,
            NARVE_EXTENSION_ID="cspjbktest1234",
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_round_trip(self):
        uid = db.create_user(
            f"rt-{int(time.time()*1000)}@test.com",
            "InitialPass123!",
            username=f"rt{int(time.time()*1000) % 10**8}",
        )
        token = extension_routes._sign_jwt(uid)["token"]
        self.assertEqual(extension_routes._verify_jwt(token), uid)

    def test_tampered_signature_rejected(self):
        uid = db.create_user(
            f"tamper-{int(time.time()*1000)}@test.com",
            "InitialPass123!",
            username=f"tamper{int(time.time()*1000) % 10**8}",
        )
        token = extension_routes._sign_jwt(uid)["token"]
        h, p, _s = token.split(".")
        forged = f"{h}.{p}.AAAA"
        self.assertIsNone(extension_routes._verify_jwt(forged))

    def test_uid_zero_rejected(self):
        """Audit §2 LOW also asked us to reject uid<=0. Cheap symmetry
        check — a sub:0 token (impossible without the secret, but a
        forged payload should not coast through)."""
        # Hand-roll a sub:0 token using the real secret so signature is
        # valid; verify must still reject on uid<=0.
        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "sub": 0,
            "iat": now,
            "exp": now + 3600,
            "scope": "extension",
            "aud": extension_routes._extension_aud(),
        }

        def _b64(d):
            raw = json.dumps(d, separators=(",", ":"), sort_keys=True).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        signing_input = f"{_b64(header)}.{_b64(payload)}".encode()
        sig = hmac.new(
            extension_routes._jwt_secret(), signing_input, hashlib.sha256
        ).digest()
        token = (
            f"{signing_input.decode()}."
            + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        )
        self.assertIsNone(extension_routes._verify_jwt(token))


if __name__ == "__main__":
    unittest.main()
