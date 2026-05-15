"""Test for admin_set_role self-demotion lockout (audit fix B).

A super-admin must not be allowed to drop their own admin_level below
their current one. The previous handler had no check, so a single
super-admin could lock themselves (and possibly the whole install,
if they were the only super-admin) out of every super-admin route
with one HTTP POST.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from urllib.parse import urlencode

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402


def _suffix() -> str:
    return f"{os.getpid()}_{int(time.time() * 1000) & 0xFFFFFF}"


def _create_super_admin() -> tuple[int, str]:
    email = f"selfdemote_{_suffix()}@test.local"
    uid = db.create_user(
        email, "Password1!verylong", username=f"selfdemote_{_suffix()}"
    )
    db.set_user_role(uid, 2)
    try:
        db.set_user_2fa_method(uid, "email_otp")
    except Exception:
        pass
    token = db.create_session(uid)
    try:
        db.mark_session_two_fa_verified(token)
    except Exception:
        pass
    return uid, token


def _prime_csrf(client: TestClient, session_token: str) -> str:
    client.get(
        "/admin/users",
        cookies={server.COOKIE_NAME: session_token},
        follow_redirects=False,
    )
    return client.cookies.get(server.CSRF_COOKIE_NAME) or ""


class AdminSelfDemoteTestCase(unittest.TestCase):
    """The `/admin/users/{id}/role` handler refuses to lower one's own role."""

    def setUp(self):
        # Per-test setUp because the conftest's autouse fixture wipes
        # users + sessions after every function-scoped test (see
        # _FIXTURE_WIPE_TABLES). A class-scoped admin would have its DB
        # rows nuked after test #1 and every subsequent test would hit
        # a CSRF/auth bounce.
        self.client = TestClient(server.app)
        self.admin_id, self.admin_token = _create_super_admin()

    def _post(self, target_uid: int, level: int):
        csrf = _prime_csrf(self.client, self.admin_token)
        body = urlencode([(server.CSRF_FORM_FIELD, csrf), ("level", str(level))])
        return self.client.post(
            f"/admin/users/{target_uid}/role",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.admin_token,
                server.CSRF_COOKIE_NAME: csrf,
            },
            follow_redirects=False,
        )

    def test_self_demote_super_to_admin_blocked(self):
        """Level 2 → 1 on self must be a 400."""
        r = self._post(self.admin_id, 1)
        self.assertEqual(r.status_code, 400, r.text[:200])
        # And the role is unchanged.
        u = db.get_user_by_id(self.admin_id)
        self.assertEqual(u["is_admin"], 2)

    def test_self_demote_super_to_user_blocked(self):
        """Level 2 → 0 on self must be a 400."""
        r = self._post(self.admin_id, 0)
        self.assertEqual(r.status_code, 400, r.text[:200])
        u = db.get_user_by_id(self.admin_id)
        self.assertEqual(u["is_admin"], 2)

    def test_self_promote_allowed(self):
        """Same level (no-op) or higher is fine — only strict demotion is blocked."""
        r = self._post(self.admin_id, 2)  # no-op
        self.assertIn(r.status_code, (302, 303), r.text[:200])

    def test_demoting_other_admin_still_works(self):
        """The check is scoped to self — managing other admins is unaffected."""
        other_email = f"otheradm_{_suffix()}@test.local"
        other_uid = db.create_user(
            other_email, "Password1!verylong", username=f"otheradm_{_suffix()}"
        )
        db.set_user_role(other_uid, 1)
        r = self._post(other_uid, 0)
        self.assertIn(r.status_code, (302, 303), r.text[:200])
        u = db.get_user_by_id(other_uid)
        self.assertEqual(u["is_admin"], 0)


if __name__ == "__main__":
    unittest.main()
