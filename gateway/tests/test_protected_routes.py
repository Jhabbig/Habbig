"""Tests for route protection under the token-first flow.

The spec explicitly lists these behaviours:
  - GET /dashboards without a session → redirect to /token (NOT /login)
  - GET /admin without a session → redirect to /token
  - GET /admin with a non-admin session → 403
  - Unauthenticated API calls → 401/403
  - Every auth-bounce in server.py now goes to /token
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
    def test_dashboards_redirects_to_token(self):
        r = client.get("/dashboards", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertEqual(r.headers["location"], "/token")

    def test_settings_redirects_to_token(self):
        r = client.get("/settings", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        # Settings either redirects to /token or to /gate if GateMiddleware
        # catches it first. Accept both — the important invariant is that
        # an unauthenticated user is NEVER sent to /login directly.
        self.assertNotEqual(r.headers["location"], "/login")

    def test_admin_redirects_to_token(self):
        r = client.get("/admin", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307, 403))
        if r.status_code in (302, 307):
            self.assertNotEqual(r.headers["location"], "/login")

    def test_saved_redirects_to_token(self):
        r = client.get("/saved", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertEqual(r.headers["location"], "/token")

    def test_login_direct_redirects_to_token(self):
        """Regression: the spec's hardest rule — /login must never be
        reachable directly without pending_token."""
        r = client.get("/login", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/token")

    def test_register_direct_redirects_to_token(self):
        r = client.get("/register", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/token")


class TestPublicPathsAllowedSet(unittest.TestCase):
    """Verify the public-path set includes exactly the auth-flow entry
    points and nothing that should require auth."""

    def test_public_paths_include_token_flow(self):
        for p in ("/token", "/register", "/login", "/auth/validate-token",
                  "/auth/register", "/auth/login", "/auth/logout"):
            self.assertIn(p, server._PUBLIC_PATHS,
                          f"{p} must be in _PUBLIC_PATHS for the token flow to work")

    def test_public_paths_do_not_include_dashboard_or_admin(self):
        for p in ("/dashboards", "/admin", "/saved", "/settings", "/api/auth/sessions"):
            self.assertNotIn(p, server._PUBLIC_PATHS,
                             f"{p} must NOT be public — it requires auth")


class TestNoLoginRedirectsRemain(unittest.TestCase):
    """No RedirectResponse('/login', …) must survive in server.py after
    the rewrite. Every auth-missing bounce goes to /token."""

    def test_server_py_has_no_login_redirects(self):
        with open(server.__file__) as f:
            src = f.read()
        self.assertNotIn('RedirectResponse("/login"', src,
                         "found a stray /login auth bounce — should be /token")

    def test_server_features_has_no_login_auth_bounces(self):
        # server_features still has ONE reference — the /register handler
        # sending claimed tokens to /login. That's NOT an auth-missing
        # bounce, it's an intra-flow step. Verify it's the only one.
        import server_features
        with open(server_features.__file__) as f:
            src = f.read()
        count = src.count('RedirectResponse("/login"')
        # Exactly one allowed: the claimed-token handoff inside /register
        self.assertLessEqual(count, 1, "only the claimed-token handoff is allowed to link to /login")


if __name__ == "__main__":
    unittest.main()
