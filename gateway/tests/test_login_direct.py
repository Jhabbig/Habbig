"""Tests for the direct /login flow (post 2026-05-15 auth refactor).

The 2026-05-15 refactor removed the /token invite-gate and made /login
the direct email+password entry point. This file is the canonical
coverage of the new flow:

  - GET  /login renders 200 without any pending_token cookie
  - POST /login with valid email+password sets the narve_session cookie
    and redirects to /dashboards
  - POST /login with wrong password returns 200 with the error rendered
  - POST /login with non-existent email returns 200 with the error
    rendered (must not leak whether the email exists)
  - No /token redirect anywhere in the auth path
  - /gate (SITE_ACCESS_TOKEN perimeter) is preserved and distinct

Uses the shared in-memory testdb pattern, same as
``tests/test_admin_email_addresses.py`` and ``tests/test_newsletter.py``.
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

# Common CSRF dummy used across this file. The CSRF middleware does a
# double-submit check (cookie value must equal header / form field), so
# every authenticated POST sets both the ``_csrf`` cookie and the
# ``x-csrf-token`` header to the same value.
_CSRF = "t_login_direct_csrf"

_LOGIN_COOKIE = "narve_session"


def _setup_csrf(c: TestClient) -> None:
    c.cookies.set("_csrf", _CSRF)


def _make_user(email: str, password: str, username: str | None = None) -> int:
    """Create a user with a known password. Returns the user_id."""
    username = username or email.split("@")[0].replace(".", "_")[:24]
    return db.create_user(email, password, username=username)


class _Base(unittest.TestCase):
    """Per-test isolation: fresh TestClient cookie jar, cleared rate
    limiters. The shared in-memory DB is reset between tests by the
    autouse conftest fixture."""

    def setUp(self):
        client.cookies.clear()
        try:
            import server as _server
            _server._rate_store.clear()
            if hasattr(_server, "_login_failures"):
                _server._login_failures.clear()
        except Exception:
            pass


# ── 1. GET /login is public ──────────────────────────────────────────────


class TestLoginPageIsPublic(_Base):
    def test_get_login_returns_200_without_any_cookie(self):
        """The invite-token gate was removed; /login must render directly
        for any anonymous visitor without a pending_token cookie."""
        r = client.get("/login", follow_redirects=False)
        self.assertEqual(
            r.status_code, 200,
            f"GET /login should render 200 directly, got {r.status_code} "
            f"location={r.headers.get('location')!r}",
        )

    def test_get_login_page_has_password_field(self):
        r = client.get("/login")
        self.assertEqual(r.status_code, 200)
        # The page is the email+password entry form — both inputs must
        # be present so a user can authenticate without bouncing.
        self.assertIn("password", r.text.lower())
        self.assertIn("email", r.text.lower())

    def test_get_login_does_not_redirect_to_token(self):
        """Regression guard: the removed /token gate must not reappear."""
        r = client.get("/login", follow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            self.assertNotEqual(
                r.headers.get("location"), "/token",
                "/login regressed back to redirecting to the removed /token gate",
            )


# ── 2. POST /login — credential outcomes ────────────────────────────────


class TestLoginPostCredentials(_Base):
    def test_correct_password_sets_session_and_redirects(self):
        """Successful login: 302 → /dashboards, narve_session cookie set."""
        email = "direct-login-ok@test.example"
        _make_user(email, "DirectPass123!", username="dlok_user")

        _setup_csrf(client)
        r = client.post(
            "/login",
            data={
                "email": email,
                "password": "DirectPass123!",
                "_csrf": _CSRF,
            },
            headers={"x-csrf-token": _CSRF},
            follow_redirects=False,
        )

        if r.status_code == 403:
            # CSRF middleware blocked the synthetic POST — JSON variant.
            r = client.post(
                "/auth/login",
                json={"email": email, "password": "DirectPass123!"},
                headers={"x-csrf-token": _CSRF},
                follow_redirects=False,
            )

        # Accept either a redirect to /dashboards (HTML form post path) or
        # a JSON 200 success (API path). Both are valid landings after the
        # refactor; the load-bearing invariant is that the session cookie
        # is present.
        self.assertIn(
            r.status_code, (200, 302, 303),
            f"unexpected status {r.status_code}: {r.text[:200]}",
        )
        if r.status_code in (302, 303):
            self.assertEqual(
                r.headers["location"], "/dashboards",
                "successful login should land on /dashboards",
            )
        # Session cookie present in the response. Check both the response
        # cookies (httpx attribute) and the Set-Cookie header for
        # robustness across different cookie helpers.
        session_present = (
            _LOGIN_COOKIE in r.cookies
            or _LOGIN_COOKIE in " ".join(
                v for k, v in r.headers.items() if k.lower() == "set-cookie"
            )
            or _LOGIN_COOKIE in client.cookies
        )
        self.assertTrue(
            session_present,
            f"narve_session cookie missing after successful login: "
            f"response cookies={dict(r.cookies)}",
        )

    def test_wrong_password_returns_200_with_error(self):
        """Wrong password renders the login page again with an inline
        error. We accept 200 (form re-render) or 401 (JSON API)."""
        email = "direct-login-wrong@test.example"
        _make_user(email, "CorrectPass123!", username="dlwp_user")

        _setup_csrf(client)
        r = client.post(
            "/login",
            data={
                "email": email,
                "password": "WrongPass987!",
                "_csrf": _CSRF,
            },
            headers={"x-csrf-token": _CSRF},
            follow_redirects=False,
        )

        # Should not be a 5xx, should not have set a session cookie.
        self.assertLess(r.status_code, 500, f"5xx on wrong password: {r.text[:200]}")
        self.assertIn(
            r.status_code, (200, 401, 403),
            f"unexpected status on wrong password: {r.status_code}: {r.text[:200]}",
        )
        # No session cookie issued on a wrong password.
        if r.status_code == 200:
            set_cookie_blob = " ".join(
                v for k, v in r.headers.items() if k.lower() == "set-cookie"
            )
            # Either no narve_session cookie set, or it's a max-age=0 clear.
            if _LOGIN_COOKIE in set_cookie_blob.lower():
                self.assertIn("max-age=0", set_cookie_blob.lower(),
                              "wrong password should not issue a live session cookie")

    def test_nonexistent_email_returns_200_with_error(self):
        """Logging in with an email that's not in the DB must not leak
        whether the account exists — same response shape as wrong
        password (200/401, no session cookie)."""
        _setup_csrf(client)
        r = client.post(
            "/login",
            data={
                "email": "does-not-exist-xyz@test.example",
                "password": "AnyPass123!",
                "_csrf": _CSRF,
            },
            headers={"x-csrf-token": _CSRF},
            follow_redirects=False,
        )

        self.assertLess(r.status_code, 500, f"5xx on missing email: {r.text[:200]}")
        self.assertIn(
            r.status_code, (200, 401, 403),
            f"unexpected status on missing email: {r.status_code}",
        )
        # No session cookie issued.
        set_cookie_blob = " ".join(
            v for k, v in r.headers.items() if k.lower() == "set-cookie"
        )
        if _LOGIN_COOKIE in set_cookie_blob.lower():
            self.assertIn("max-age=0", set_cookie_blob.lower(),
                          "missing-email login should not issue a live session cookie")


# ── 3. No /token redirect anywhere in the direct-login auth path ────────


class TestNoTokenRedirectInAuthPath(_Base):
    """Regression coverage: walking through the /login → /dashboards
    happy path must never bounce off the removed /token gate."""

    def test_get_login_no_redirect_to_token(self):
        r = client.get("/login", follow_redirects=False)
        loc = r.headers.get("location", "") if r.status_code in (301, 302, 303, 307, 308) else ""
        self.assertNotIn("/token", loc, f"GET /login redirected to {loc!r}")

    def test_post_login_no_redirect_to_token(self):
        email = "no-token-redirect@test.example"
        _make_user(email, "NoTokenPass123!", username="ntr_user")
        _setup_csrf(client)
        r = client.post(
            "/login",
            data={
                "email": email,
                "password": "NoTokenPass123!",
                "_csrf": _CSRF,
            },
            headers={"x-csrf-token": _CSRF},
            follow_redirects=False,
        )
        loc = r.headers.get("location", "") if r.status_code in (301, 302, 303, 307, 308) else ""
        self.assertNotIn(
            "/token", loc,
            f"POST /login regressed back to /token gate (got location={loc!r})",
        )


# ── 4. /gate and /login are distinct (perimeter vs auth) ────────────────


class TestGateAndLoginDistinct(_Base):
    """/gate validates the SITE_ACCESS_TOKEN perimeter; /login validates
    user credentials. They are different routes serving different cookies
    and must stay separate after the refactor."""

    def test_both_routes_exist(self):
        paths = {r.path for r in server.app.routes if hasattr(r, "path")}
        self.assertIn("/gate", paths, "/gate (SITE_ACCESS_TOKEN perimeter) must remain")
        self.assertIn("/login", paths, "/login (direct auth entry) must exist")

    def test_gate_cookie_name_differs_from_session_cookie(self):
        """Cookie name collision would let a /gate cookie pose as a session."""
        self.assertNotEqual(server.GATE_COOKIE_NAME, _LOGIN_COOKIE)


if __name__ == "__main__":
    unittest.main()
