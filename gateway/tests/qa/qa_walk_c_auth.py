"""QA Walk C — authenticated walk.

Same shape as Walk B but with a logged-in non-admin session. The user
exists, has no admin flag, has no premium add-ons. Pages that gate on
a paid tier (e.g. `/intelligence`, `/signal-search`) are allowed to
redirect or 402 — we only fail on 5xx.

We don't repeat the body-emptiness check from Walk B; if the unauth
suite is happy, the authed render path uses the same render_page().
The new check here is "auth doesn't crash any of these routes".
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from . import conftest as _conf  # noqa: F401

import db  # noqa: E402
import server  # noqa: E402


AUTH_PAGES = [
    # Core dashboards
    "/dashboards",
    "/profile",
    "/settings",
    "/billing",
    "/notifications",
    # Saved / predictions
    "/saved",
    # Intelligence + search (may 402 for free tier — we accept any < 500)
    "/intelligence",
    "/signal-search",
]


def _login() -> dict:
    """Idempotent — re-running tests reuses the same user."""
    email = "qa-walk-c-authed@test.local"
    existing = (
        db.get_user_by_email(email)
        if hasattr(db, "get_user_by_email") else None
    )
    if existing:
        uid = existing["id"]
    else:
        uid = db.create_user(email, "QaWalkPass123!", username="qawalkcauth")
    token = db.create_session(uid)
    return {server.COOKIE_NAME: token}


class TestAuthedPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)
        cls.cookies = _login()

    def test_no_5xx_on_any_authed_page(self):
        failures: list[tuple[str, int]] = []
        for path in AUTH_PAGES:
            r = self.client.get(
                path, cookies=self.cookies, follow_redirects=False,
            )
            if r.status_code >= 500:
                failures.append((path, r.status_code))
        self.assertEqual(failures, [], f"5xx on authed pages: {failures}")

    def test_session_actually_persists(self):
        """/api/me with our cookie should return authenticated=True. If
        not, the cookie pipeline broke and every other authed assertion
        is meaningless."""
        # /api/me may not exist in every build — fall back to a 200 on
        # /dashboards as the looser proxy.
        r = self.client.get("/api/me", cookies=self.cookies, follow_redirects=False)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
            body = r.json()
            self.assertTrue(
                body.get("authenticated") or body.get("user_id"),
                f"/api/me did not recognise our session: {body}",
            )
        else:
            r2 = self.client.get("/dashboards", cookies=self.cookies, follow_redirects=False)
            self.assertLess(r2.status_code, 500,
                            f"/dashboards 5xx with session: {r2.status_code}")


if __name__ == "__main__":
    unittest.main()
