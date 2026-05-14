"""QA Walk D — admin walk.

Two parametrized loops:

  * with admin cookies → page must not 5xx (200 / 302 / 303 OK)
  * with non-admin cookies → page MUST be denied (403, or a 200 that
    renders the denied template — admin pages use the page=True
    branch which returns the friendly denied page rather than raising)

Pages that gate on extra factors (admin 2FA fresh, super-admin
quorum, …) may 303 to a 2FA verify route under the admin variant.
We accept that — it's a security feature, not a 5xx.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from . import conftest as _conf  # noqa: F401

import db  # noqa: E402
import server  # noqa: E402


# Admin paths the codebase actually serves — verified via grep over
# server.py + admin_routes.py. Routes that don't exist in this build
# are dropped; tests for present-but-feature-flagged pages would be
# better written as unit tests in their owner modules.
ADMIN_PAGES = [
    "/admin",
    "/admin/cache",
    "/admin/backups",
    "/admin/flags",
    "/admin/emails",
    "/admin/email-templates",
    "/admin/impersonations",
    "/admin/audit-log",
    "/admin/logs/errors",
    "/admin/logs/live",
    "/admin/logs/search",
    "/admin/sharing",
    "/admin/churn",
    "/admin/subproducts",
    "/admin/search-analytics",
]


def _setup_users() -> tuple[dict, dict]:
    """Idempotent — both users persist across runs."""
    admin_email = "qa-walk-d-admin@test.local"
    user_email = "qa-walk-d-user@test.local"

    admin_existing = (
        db.get_user_by_email(admin_email)
        if hasattr(db, "get_user_by_email") else None
    )
    if admin_existing:
        admin_uid = admin_existing["id"]
    else:
        admin_uid = db.create_user(
            admin_email, "QaWalkPass123!", username="qawalkdadmin",
        )
    with db.conn() as c:
        c.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (admin_uid,))

    user_existing = (
        db.get_user_by_email(user_email)
        if hasattr(db, "get_user_by_email") else None
    )
    if user_existing:
        user_uid = user_existing["id"]
    else:
        user_uid = db.create_user(
            user_email, "QaWalkPass123!", username="qawalkduser",
        )
    with db.conn() as c:
        c.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (user_uid,))

    return (
        {server.COOKIE_NAME: db.create_session(admin_uid)},
        {server.COOKIE_NAME: db.create_session(user_uid)},
    )


class TestAdminPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)
        cls.admin_cookies, cls.user_cookies = _setup_users()

    def test_admin_pages_no_5xx_for_admin(self):
        """Each admin path must not 5xx when hit by a real admin."""
        bad: list[tuple[str, int]] = []
        for path in ADMIN_PAGES:
            r = self.client.get(
                path, cookies=self.admin_cookies, follow_redirects=False,
            )
            # 303 = 2FA redirect, fine. 404 = page not in this build, fine.
            # 5xx = real bug.
            if r.status_code >= 500:
                bad.append((path, r.status_code))
        self.assertEqual(
            bad, [],
            f"5xx on admin paths under admin session: {bad}",
        )

    def test_admin_pages_denied_for_non_admin(self):
        """Non-admin must NOT see admin content. Acceptable responses:
          * 403 (raise path)
          * 302/303 (redirect to login or denied page)
          * 200 with the denied template (page=True branch)
        Failure: 200 with admin content body, OR 5xx."""
        leaks: list[tuple[str, int]] = []
        for path in ADMIN_PAGES:
            r = self.client.get(
                path, cookies=self.user_cookies, follow_redirects=False,
            )
            if r.status_code >= 500:
                leaks.append((path, r.status_code))
                continue
            if r.status_code == 200:
                # Heuristic: an admin-content body has at least one of
                # these phrases. The denied template doesn't.
                body = r.text.lower()
                admin_markers = ("impersonation", "feature flag",
                                 "audit log", "cache stats", "drill_runs",
                                 "search-analytics", "churn risk")
                if any(m in body for m in admin_markers):
                    leaks.append((path, 200))
        self.assertEqual(
            leaks, [],
            f"non-admin saw admin content / hit 5xx: {leaks}",
        )


if __name__ == "__main__":
    unittest.main()
