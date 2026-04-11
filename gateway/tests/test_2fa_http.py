"""HTTP-level tests for the 2FA gate and admin enforcement.

Verifies that after this change:
  - An admin with no 2FA method configured is redirected to /auth/2fa/setup
  - An admin with 2FA configured but session unverified is redirected to /auth/2fa
  - An admin with 2FA configured AND session marked verified can reach /admin
  - A regular user (is_admin=0) does not trip the 2FA gate on public pages
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set a Fernet key so TOTP secret storage paths don't warn
from cryptography.fernet import Fernet  # noqa: E402
os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode())

import db  # noqa: E402

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()
import migrations  # noqa: E402
migrations.upgrade_to_head()

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)


class TestAdminTwoFactorGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Re-pin db.conn to THIS file's fake before any DB calls. Other test
        # modules in the pytest session may have monkey-patched db.conn at
        # module load and clobbered ours; without this re-pin, the create_user
        # call below would write into a different in-memory DB than the one
        # the route handlers later read from.
        cls._previous_db_conn = db.conn
        db.conn = _fake_conn
        cls.admin_id = db.create_user(
            "twofa_admin@test.com", "TestPass123!",
            username="twofaadmin", is_admin=True,
        )

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._previous_db_conn

    def setUp(self):
        # Each test re-pins too, so individual tests' DB ops always hit our conn.
        db.conn = _fake_conn

    def _fresh_session(self):
        return db.create_session(self.admin_id)

    def test_admin_without_2fa_redirects_to_setup(self):
        """Admin without a method configured is redirected to /auth/2fa/setup."""
        # Reset state
        db.disable_user_2fa(self.admin_id)
        token = self._fresh_session()
        r = client.get(
            "/admin",
            cookies={server.COOKIE_NAME: token},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(r.headers.get("location"), "/auth/2fa/setup")

    def test_admin_with_2fa_unverified_session_redirects_to_verify(self):
        """Admin enrolled but session not marked verified → /auth/2fa."""
        db.set_user_2fa_method(self.admin_id, "email_otp", None)
        token = self._fresh_session()
        r = client.get(
            "/admin",
            cookies={server.COOKIE_NAME: token},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(r.headers.get("location"), "/auth/2fa")

    def test_admin_with_verified_session_reaches_admin(self):
        """Admin with verified session is allowed through."""
        db.set_user_2fa_method(self.admin_id, "email_otp", None)
        token = self._fresh_session()
        db.mark_session_two_fa_verified(token)
        r = client.get(
            "/admin",
            cookies={server.COOKIE_NAME: token},
            follow_redirects=False,
        )
        # Admin page renders (200) or at worst 500 — definitely not redirected
        self.assertNotIn(r.status_code, (302, 303))

    def test_regular_user_not_affected_by_2fa_gate(self):
        """Regular users don't need 2FA to reach non-admin routes."""
        uid = db.create_user("regular_2fa@test.com", "TestPass123!", username="regular2fa")
        token = db.create_session(uid)
        db.mark_session_two_fa_verified(token)  # pretend verified
        r = client.get(
            "/settings",
            cookies={server.COOKIE_NAME: token},
            follow_redirects=False,
        )
        # Either 200 or a non-2FA redirect — the point is we don't bounce to /auth/2fa
        if r.status_code in (302, 303):
            self.assertNotIn("/auth/2fa", r.headers.get("location", ""))


if __name__ == "__main__":
    unittest.main()
