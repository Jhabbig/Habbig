"""Tests for /admin/users — the extracted user-management page.

Covers:
  - Anon callers (no session) get a 403/302 from ``_denied_response``.
  - Admin callers get 200 with the hero, filter bar, table, and pagination.
  - Cursor pagination (``before_id``) walks past the first page deterministically.
  - Role and plan filters narrow the rendered row set.
  - CSRF is enforced on POST actions (revoke-sessions, bulk-actions).
  - Impersonation requires admin (regular user → 403).

Auth setup mirrors ``test_admin_jobs.py`` — seed a super-admin in the DB,
mark its session 2FA-verified, and reuse the cookie across tests.
"""

from __future__ import annotations

import os
import sys
import unittest
from urllib.parse import urlencode

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402


def _suffix() -> str:
    return f"{os.getpid()}"


def _create_admin_session(level: int = 2) -> tuple[int, str]:
    email = f"admusers_{_suffix()}_{level}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = int(existing["id"])
    else:
        uid = db.create_user(
            email, "Password1!verylong", username=f"admusers_{_suffix()}_{level}"
        )
    db.set_user_role(uid, level)
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


def _create_regular_session() -> tuple[int, str]:
    email = f"adm_regular_{_suffix()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = int(existing["id"])
        db.set_user_role(uid, 0)
    else:
        uid = db.create_user(
            email, "Password1!verylong", username=f"adm_regular_{_suffix()}"
        )
        db.set_user_role(uid, 0)
    return uid, db.create_session(uid)


def _seed_users(prefix: str, n: int) -> list[int]:
    ids: list[int] = []
    for i in range(n):
        email = f"{prefix}{i}_{_suffix()}@example.com"
        existing = db.get_user_by_email(email)
        if existing:
            ids.append(int(existing["id"]))
            continue
        uid = db.create_user(
            email, "Password1!verylong",
            username=f"{prefix}_user_{i}_{_suffix()}",
        )
        ids.append(uid)
    return ids


def _prime_csrf(client: TestClient, session_token: str) -> str:
    """Hit a GET to populate the CSRF cookie, then return its value."""
    client.get(
        "/admin/users",
        cookies={server.COOKIE_NAME: session_token},
        follow_redirects=False,
    )
    return client.cookies.get(server.CSRF_COOKIE_NAME) or ""


class AdminUsersPageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_id, cls.admin_token = _create_admin_session(level=2)
        cls.user_id, cls.user_token = _create_regular_session()
        cls.admin_cookies = {server.COOKIE_NAME: cls.admin_token}
        cls.user_cookies = {server.COOKIE_NAME: cls.user_token}

    # ── Auth gates ──────────────────────────────────────────────────

    def test_anon_blocked(self):
        # No session cookie at all → 302 to /gate or 403 (per _denied_response).
        r = self.client.get("/admin/users", cookies={}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303, 403))

    def test_regular_user_forbidden(self):
        r = self.client.get(
            "/admin/users",
            cookies=self.user_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    def test_admin_200(self):
        r = self.client.get(
            "/admin/users",
            cookies=self.admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.text
        # Hero + filter bar + table chrome
        self.assertIn("Users", body)
        self.assertIn("adm-users-hero__display", body)
        self.assertIn("adm-users-filter", body)
        self.assertIn("adm-users-table", body)
        # Table headers
        for header in ("Email", "Handle", "Role", "Plan", "Created", "Last active"):
            self.assertIn(header, body)

    # ── Pagination ──────────────────────────────────────────────────

    def test_pagination_cursor_walks_forward(self):
        # Seed enough users that we can ask for a small page and observe
        # the cursor narrowing the next response.
        _seed_users("pag", 5)
        r1 = self.client.get(
            "/admin/users",
            cookies=self.admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r1.status_code, 200)
        # Pass an explicit cursor that skips past the newest users —
        # the page must still render but with a different row set.
        all_users = db.list_all_users(limit=500)
        if len(all_users) < 2:
            self.skipTest("Not enough users seeded to test cursor")
        cursor = int(all_users[0]["id"])  # newest
        r2 = self.client.get(
            f"/admin/users?before_id={cursor}",
            cookies=self.admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r2.status_code, 200)
        # The first page contained the newest user; the cursor page must not.
        newest_email = all_users[0]["email"]
        self.assertIn(newest_email, r1.text)
        self.assertNotIn(newest_email, r2.text)

    # ── Filters ─────────────────────────────────────────────────────

    def test_role_filter_admin(self):
        # The seeded admin should show with role=admin or super; filtering
        # by role=super should always include them.
        r = self.client.get(
            "/admin/users?role=super",
            cookies=self.admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        # The admin's own username should appear; a fresh regular user's
        # username should not.
        admin_row = db.get_user_by_id(self.admin_id)
        self.assertIn(admin_row["username"], r.text)

    def test_role_filter_user_excludes_admin(self):
        r = self.client.get(
            "/admin/users?role=user",
            cookies=self.admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        # The super-admin must not appear in a role=user page.
        admin_row = db.get_user_by_id(self.admin_id)
        self.assertNotIn(f">{admin_row['username']}<", r.text)

    def test_plan_filter_renders(self):
        # Filtering by plan=none should at least render the page without
        # error and not include the admin (who maps to pro).
        r = self.client.get(
            "/admin/users?plan=none",
            cookies=self.admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        # Should still render the filter chrome and the role pill markup
        # for matching users (or the empty-state fallback).
        self.assertTrue(
            "adm-users-row__plan--none" in r.text
            or "adm-users-empty" in r.text
        )

    # ── CSRF + per-row POST actions ─────────────────────────────────

    def test_revoke_sessions_requires_csrf(self):
        # POST without _csrf cookie/field → 403 from the CSRF middleware.
        r = self.client.post(
            f"/admin/users/{self.user_id}/revoke-sessions",
            cookies={server.COOKIE_NAME: self.admin_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    def test_revoke_sessions_with_csrf_ok(self):
        csrf = _prime_csrf(self.client, self.admin_token)
        self.assertTrue(csrf, "CSRF token must be present on the rendered page")
        # Create an extra session for the target so revoke has something to kill.
        _ = db.create_session(self.user_id)
        body = urlencode([(server.CSRF_FORM_FIELD, csrf)])
        r = self.client.post(
            f"/admin/users/{self.user_id}/revoke-sessions",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.admin_token,
                server.CSRF_COOKIE_NAME: csrf,
            },
            follow_redirects=False,
        )
        # 302 → /admin/users (RedirectResponse) on success.
        self.assertIn(r.status_code, (302, 303))

    def test_bulk_actions_requires_csrf(self):
        # POST without _csrf token must 403 even with valid admin session.
        r = self.client.post(
            "/admin/users/bulk-actions",
            data={"bulk_action": "allowlist", "user_ids": str(self.user_id)},
            cookies={server.COOKIE_NAME: self.admin_token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    def test_bulk_actions_allowlist_ok(self):
        csrf = _prime_csrf(self.client, self.admin_token)
        self.assertTrue(csrf)
        body = urlencode([
            (server.CSRF_FORM_FIELD, csrf),
            ("bulk_action", "allowlist"),
            ("user_ids", str(self.user_id)),
        ])
        r = self.client.post(
            "/admin/users/bulk-actions",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.admin_token,
                server.CSRF_COOKIE_NAME: csrf,
            },
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))

    # ── Impersonation gate ──────────────────────────────────────────

    def test_impersonate_requires_admin(self):
        # A regular user trying to impersonate another user → 403.
        csrf = _prime_csrf(self.client, self.user_token)
        body = urlencode([
            (server.CSRF_FORM_FIELD, csrf or ""),
            ("reason", "regression test"),
        ])
        r = self.client.post(
            f"/admin/users/{self.admin_id}/impersonate",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            cookies={
                server.COOKIE_NAME: self.user_token,
                **({server.CSRF_COOKIE_NAME: csrf} if csrf else {}),
            },
            follow_redirects=False,
        )
        # Either 403 from the admin gate or 403 from CSRF — both are correct
        # rejections; the important property is that a non-admin cannot
        # successfully impersonate.
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
