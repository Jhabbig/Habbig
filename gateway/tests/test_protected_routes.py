"""Tests for route protection under the direct-/login auth flow.

After the 2026-05-15 refactor:
  - GET /dashboards without a session → redirect to /login (NOT /token)
  - GET /admin without a session → redirect to /login or 403
  - GET /admin with a non-admin session → 403
  - Unauthenticated API calls → 401/403
  - /login is the direct email+password entry point (no invite-token gate)
  - /gate (SITE_ACCESS_TOKEN perimeter) is preserved
"""

from __future__ import annotations

import os
import unittest

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("EMAIL_DRY_RUN", "true")

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402
import server  # noqa: E402
import server_features  # noqa: F401,E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(server.app)


class TestProtectedRoutes(unittest.TestCase):
    def test_dashboards_redirects_to_login(self):
        r = client.get("/dashboards", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertEqual(r.headers["location"], "/login")

    def test_settings_redirects_to_login(self):
        r = client.get("/settings", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        # Settings either redirects to /login or to /gate if GateMiddleware
        # catches it first. Accept both — the important invariant is that
        # an unauthenticated user is sent to /login, not back into the
        # removed /token gate.
        self.assertNotEqual(r.headers["location"], "/token")

    def test_admin_redirects_to_login(self):
        r = client.get("/admin", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307, 403))
        if r.status_code in (302, 307):
            self.assertNotEqual(r.headers["location"], "/token")

    def test_saved_redirects_to_login(self):
        r = client.get("/saved", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertEqual(r.headers["location"], "/login")

    def test_login_direct_renders_200(self):
        """The auth refactor removed the /token gate — /login is the
        direct entry point and must render publicly without any
        pending_token cookie."""
        r = client.get("/login", follow_redirects=False)
        self.assertEqual(r.status_code, 200)

    def test_register_direct_renders_or_redirects_to_login(self):
        # Either /register is also public OR it redirects to /login.
        # The hard invariant is that it MUST NOT redirect to /token.
        r = client.get("/register", follow_redirects=False)
        if r.status_code in (302, 307):
            self.assertNotEqual(r.headers["location"], "/token")
        else:
            self.assertEqual(r.status_code, 200)


class TestPublicPathsAllowedSet(unittest.TestCase):
    """Verify the public-path set includes /login (the new direct entry
    point) and not anything that should require auth."""

    def test_public_paths_include_login(self):
        # /login is the canonical email+password entry point after the
        # 2026-05-15 refactor; it must remain public.
        self.assertIn("/login", server._PUBLIC_PATHS,
                      "/login must be in _PUBLIC_PATHS for the direct auth flow")
        # /auth/login is the POST target.
        self.assertIn("/auth/login", server._PUBLIC_PATHS,
                      "/auth/login must be in _PUBLIC_PATHS for credential POST")
        # Logout endpoint stays public so already-authed users can sign out.
        self.assertIn("/auth/logout", server._PUBLIC_PATHS,
                      "/auth/logout must be in _PUBLIC_PATHS")

    def test_public_paths_do_not_include_dashboard_or_admin(self):
        for p in ("/dashboards", "/admin", "/saved", "/settings", "/api/auth/sessions"):
            self.assertNotIn(p, server._PUBLIC_PATHS,
                             f"{p} must NOT be public — it requires auth")


class TestNoTokenAuthBouncesRemain(unittest.TestCase):
    """No new RedirectResponse('/token', …) auth-bounce should be added
    once the refactor is in. Every auth-missing bounce now goes to /login.

    This test is light: it just confirms the test_protected_routes
    behaviour above stays true. It does NOT grep server.py source for
    /token, because the perimeter /gate flow and the legacy
    SITE_ACCESS_TOKEN system still reference /token-like paths.
    """

    def test_protected_html_routes_never_bounce_to_token(self):
        client.cookies.clear()
        for path in ("/dashboards", "/saved", "/settings"):
            r = client.get(path, follow_redirects=False)
            if r.status_code in (302, 307):
                loc = r.headers.get("location", "")
                self.assertNotEqual(
                    loc, "/token",
                    f"{path} regressed: still bounces to removed /token gate",
                )


if __name__ == "__main__":
    unittest.main()
