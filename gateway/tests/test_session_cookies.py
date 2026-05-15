"""Session cookie security tests.

After the 2026-05-15 auth refactor the pending_token cookie was removed
along with the /token gate. Test classes covering pending_token cookie
attributes are SKIPPED but kept on disk for cheap rollback. Session
cookie tests are unchanged — the hardened-session contract is identical
under the direct /login flow.

Live tests:
  - Session cookie is HttpOnly, Secure, SameSite=Strict
  - Session stored as SHA-256 hash in DB, not raw
  - Valid session cookie → user attached to request.state
  - Expired session → validate returns None
  - Revoked session → validate returns None
  - Session last_active_at updated on each lookup
  - rotate_session revokes old + issues new on privilege change
"""

from __future__ import annotations

import hashlib
import os
import time
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
# NB: we deliberately do NOT set PRODUCTION=1 at module-import time —
# other test modules imported during collection would inherit it and
# break. Each cookie-attribute test sets + clears the env var in its
# own setUp/tearDown scope instead.

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402
# pending_token helpers are imported lazily inside the skip-marked
# classes so this module still imports cleanly after the refactor
# removes them from auth.cookies.
from auth.cookies import (  # noqa: E402
    SESSION_COOKIE,
    SESSION_COOKIE_TTL,
    set_session_cookie_hardened,
)

_REMOVED = "pending_token cookie removed 2026-05-15; /login is now direct"


def _fake_request():
    """Minimal object with the attributes our cookie helpers need."""
    class FR:
        headers: dict = {}
        cookies: dict = {}
    return FR()


def _fake_response():
    """FastAPI Response stub that records cookies in a dict the same way
    Starlette's Response.set_cookie does."""
    from fastapi import Response
    return Response()


class TestSessionCookieAttributes(unittest.TestCase):
    """Spec: session cookie MUST be HttpOnly, Secure, SameSite=Strict."""

    def setUp(self):
        # `auth.cookies._is_production()` reads PRODUCTION on every call,
        # so setting it here guarantees the Secure flag regardless of how
        # other test modules interact with the env.
        self._saved_prod = os.environ.get("PRODUCTION")
        os.environ["PRODUCTION"] = "1"

    def tearDown(self):
        if self._saved_prod is None:
            os.environ.pop("PRODUCTION", None)
        else:
            os.environ["PRODUCTION"] = self._saved_prod

    def test_session_cookie_is_httponly_secure_strict(self):
        request = _fake_request()
        response = _fake_response()
        set_session_cookie_hardened(response, "raw-session-value", request)

        # Starlette stores the cookie as a header on the response
        set_cookie_headers = [
            v for k, v in response.headers.items() if k.lower() == "set-cookie"
        ]
        self.assertTrue(set_cookie_headers, "no Set-Cookie header emitted")
        header = set_cookie_headers[0]
        self.assertIn("narve_session=", header)
        self.assertIn("HttpOnly", header)
        self.assertIn("Secure", header)
        # SameSite=Strict
        self.assertIn("SameSite=strict".lower(), header.lower())

    def test_session_cookie_has_7_day_max_age(self):
        request = _fake_request()
        response = _fake_response()
        set_session_cookie_hardened(response, "raw", request)
        set_cookie_headers = [
            v for k, v in response.headers.items() if k.lower() == "set-cookie"
        ]
        header = set_cookie_headers[0]
        self.assertIn(f"Max-Age={SESSION_COOKIE_TTL}", header)
        # Sanity — the TTL really is 7 days
        self.assertEqual(SESSION_COOKIE_TTL, 7 * 24 * 60 * 60)


@unittest.skip(_REMOVED)
class TestPendingTokenCookieAttributes(unittest.TestCase):
    """Spec: pending_token cookie is NOT HttpOnly and Max-Age 30 min.

    Skipped: pending_token cookie was removed along with the /token gate
    in the 2026-05-15 auth refactor. /login is now the direct entry
    point and doesn't depend on a pre-login cookie.
    """

    def setUp(self):
        self._saved_prod = os.environ.get("PRODUCTION")
        os.environ["PRODUCTION"] = "1"

    def tearDown(self):
        if self._saved_prod is None:
            os.environ.pop("PRODUCTION", None)
        else:
            os.environ["PRODUCTION"] = self._saved_prod

    def test_pending_token_cookie_is_not_httponly(self):
        pass  # see class skip

    def test_pending_token_cookie_has_30_min_max_age(self):
        pass  # see class skip

    def test_pending_token_value_is_signed(self):
        pass  # see class skip


class TestSessionStoredAsHashNotRaw(unittest.TestCase):
    def test_raw_token_never_appears_in_db(self):
        uid = db.create_user("cookie-hash@test.com", "Pass123!@#", username="cookiehash1")
        raw = db.create_user_session(uid)
        with db.conn() as c:
            rows = c.execute(
                "SELECT token_hash FROM user_sessions WHERE user_id = ?", (uid,),
            ).fetchall()
        for row in rows:
            self.assertNotEqual(row["token_hash"], raw,
                                "user_sessions.token_hash must be a hash, not the raw value")
        # And the hash matches sha256(raw)
        expected = hashlib.sha256(raw.encode()).hexdigest()
        hashes = [r["token_hash"] for r in rows]
        self.assertIn(expected, hashes)


class TestSessionValidationAttachesToRequestState(unittest.TestCase):
    """Spec: valid session cookie → user attached to request.state."""

    def test_middleware_attaches_user_to_request_state(self):
        # Build a request with the hardened cookie set and run the middleware
        # helper directly, since TestClient doesn't persist custom request.state
        # between manual calls.
        uid = db.create_user("state-attach@test.com", "Pass123!@#", username="stateattach1")
        raw = db.create_user_session(uid, ip_address="8.8.8.8", user_agent="ua")

        from auth.guards import attach_session_to_request

        class FakeState:
            pass

        class FakeRequest:
            cookies = {SESSION_COOKIE: raw}
            state = FakeState()

        request = FakeRequest()
        attach_session_to_request(request)
        self.assertIsNotNone(request.state.hardened_user)
        self.assertEqual(request.state.hardened_user["user_id"], uid)
        self.assertEqual(request.state.hardened_user["email"], "state-attach@test.com")


class TestSessionValidationExpired(unittest.TestCase):
    def test_expired_session_returns_none(self):
        uid = db.create_user("exp@test.com", "Pass123!@#", username="expcookie1")
        raw = db.create_user_session(uid, ttl_seconds=1)
        time.sleep(1.2)
        self.assertIsNone(db.validate_user_session(raw))


class TestSessionValidationRevoked(unittest.TestCase):
    def test_revoked_session_returns_none(self):
        uid = db.create_user("rev-cookie@test.com", "Pass123!@#", username="revcookie1")
        raw = db.create_user_session(uid)
        db.revoke_user_session_by_token(raw)
        self.assertIsNone(db.validate_user_session(raw))


class TestSessionLastActiveUpdated(unittest.TestCase):
    """Spec: last_active_at updated on each validation."""

    def test_last_active_bumps_forward(self):
        uid = db.create_user("la@test.com", "Pass123!@#", username="lacookie1")
        raw = db.create_user_session(uid)
        hash_hex = hashlib.sha256(raw.encode()).hexdigest()
        with db.conn() as c:
            first = c.execute(
                "SELECT last_active_at FROM user_sessions WHERE token_hash = ?",
                (hash_hex,),
            ).fetchone()["last_active_at"]
        time.sleep(1.1)
        # validate_user_session writes the bump before returning, so a
        # fresh read picks it up even though the returned row is pre-bump.
        self.assertIsNotNone(db.validate_user_session(raw))
        with db.conn() as c:
            second = c.execute(
                "SELECT last_active_at FROM user_sessions WHERE token_hash = ?",
                (hash_hex,),
            ).fetchone()["last_active_at"]
        self.assertGreater(second, first)


class TestRotateSessionHelper(unittest.TestCase):
    """Spec STEP 9: rotate_session revokes old + issues new on privilege change."""

    def test_rotate_session_revokes_old_and_issues_new(self):
        uid = db.create_user("rotate@test.com", "Pass123!@#", username="rotatec1")
        old = db.create_user_session(uid, ip_address="1.1.1.1", user_agent="ua")
        self.assertIsNotNone(db.validate_user_session(old))

        new = db.rotate_session(old, uid, ip_address="1.1.1.1", user_agent="ua")
        self.assertIsNotNone(new)
        self.assertNotEqual(new, old)
        # Old is dead
        self.assertIsNone(db.validate_user_session(old))
        # New works
        row = db.validate_user_session(new)
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], uid)

    def test_rotate_session_rejects_wrong_user_id(self):
        uid_a = db.create_user("rota@test.com", "Pass123!@#", username="rotaca1")
        uid_b = db.create_user("rotb@test.com", "Pass123!@#", username="rotacb1")
        old = db.create_user_session(uid_a)
        # Someone tries to rotate A's session into B's account
        result = db.rotate_session(old, uid_b)
        self.assertIsNone(result)
        # A's session is still valid
        self.assertIsNotNone(db.validate_user_session(old))

    def test_rotate_session_on_unknown_token_returns_none(self):
        self.assertIsNone(db.rotate_session("does-not-exist", 42))
        self.assertIsNone(db.rotate_session("", 42))


if __name__ == "__main__":
    unittest.main()
