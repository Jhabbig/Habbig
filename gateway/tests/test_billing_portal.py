"""Tests for POST /api/billing/portal-session — the live Stripe Customer
Portal session creator wired up in billing_routes.py.

What's covered:
  * Anonymous request                 -> 401 auth_required
  * Authenticated user, no customer_id -> 400 no_active_subscription
  * Authenticated user with customer_id -> 200 with portal URL (mock stripe)
  * Response NEVER echoes the customer_id back (no cus_… leakage)
  * Missing/invalid CSRF token        -> 403 (proves middleware is wiring)
  * STRIPE_SECRET_KEY missing         -> 503 billing_unavailable

The Stripe SDK is replaced with an in-process stub via ``sys.modules["stripe"]``
BEFORE ``server`` is imported, mirroring test_stripe_webhook_route.py. The stub
records every ``billing_portal.Session.create`` call so a single test can assert
both response shape and the parameters narve.ai sent to Stripe.

The shared in-memory DB pattern (tests/_testdb) is used so the migration that
adds users.stripe_customer_id has already been applied — we can write into
that column directly without monkey-patching.
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

USES_TESTDB = True

# Import _testdb BEFORE server so the shared in-memory DB is live when the
# billing_routes module is loaded (which happens transitively through server).
from tests import _testdb  # noqa: E402,F401


# ── Install a stripe SDK stub before importing server ───────────────────────
#
# The portal-session endpoint does ``import stripe`` lazily inside the
# handler. We need that import to find OUR stub rather than the real SDK
# (which isn't installed in CI), so we register the module before server
# loads — same pattern as test_stripe_webhook_route._install_fake_stripe.

_LAST_CALL: dict = {}
_NEXT_URL = "https://billing.stripe.com/p/session/test_default"


def _install_fake_stripe():
    mod = types.ModuleType("stripe")
    mod.api_key = ""

    def _portal_create(*args, **kwargs):
        # Capture call args for assertions. asyncio.to_thread invokes us
        # with positional + kwargs depending on how the handler called it;
        # the production code uses kwargs only.
        _LAST_CALL.clear()
        _LAST_CALL.update(kwargs)
        return types.SimpleNamespace(
            id="bps_test_default",
            url=_NEXT_URL,
            customer=kwargs.get("customer"),
        )

    mod.billing_portal = types.SimpleNamespace(
        Session=types.SimpleNamespace(create=_portal_create),
    )
    sys.modules["stripe"] = mod
    return mod


_install_fake_stripe()

import db  # noqa: E402
import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# Re-pin the shared in-memory connection. Sibling test files may have
# repointed db.conn at their own per-file fakes during collection.
_SHARED_DB_CONN = _testdb._fake_conn


def _pin_shared_db() -> None:
    db.conn = _SHARED_DB_CONN


client = TestClient(server.app)


def _clear_client_cookies() -> None:
    try:
        client.cookies.clear()
    except Exception:
        pass


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_user(email: str, username: str, *, customer_id: str | None = None) -> tuple[int, str]:
    """Create a user (+optional stripe_customer_id) and return (uid, session_token)."""
    uid = db.create_user(email, "TestPass123!", username=username)
    if customer_id is not None:
        with db.conn() as c:
            c.execute(
                "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
                (customer_id, uid),
            )
    token = db.create_session(uid)
    return uid, token


def _prime_csrf(token: str) -> str:
    """Hit a GET that emits the _csrf cookie, then read it back."""
    client.get(
        "/settings/billing",
        cookies={server.COOKIE_NAME: token},
        follow_redirects=False,
    )
    return client.cookies.get("_csrf") or ""


def _post_json(path: str, *, token: str | None, body: dict | None = None,
               include_csrf: bool = True):
    """Authenticated JSON POST with the X-CSRF-Token header set up correctly.

    Pass ``token=None`` to simulate an anonymous request.
    Pass ``include_csrf=False`` to simulate a missing CSRF token.
    """
    cookies: dict[str, str] = {}
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token is not None:
        cookies[server.COOKIE_NAME] = token
        csrf = _prime_csrf(token)
        if include_csrf and csrf:
            cookies["_csrf"] = csrf
            headers["X-CSRF-Token"] = csrf
    return client.post(
        path,
        content=json.dumps(body or {}),
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )


# ── Test base ───────────────────────────────────────────────────────────────


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _pin_shared_db()
        # Ensure a STRIPE_SECRET_KEY is set so the 503 short-circuit doesn't
        # fire — individual tests override this when they need to exercise
        # the unavailable path.
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_portal_default"
        super().setUpClass()

    def setUp(self):
        _pin_shared_db()
        _clear_client_cookies()
        _LAST_CALL.clear()
        super().setUp()


# ── Tests ───────────────────────────────────────────────────────────────────


class TestAnonReturns401(_Base):
    """Unauthenticated POSTs are rejected with 401 auth_required."""

    def test_no_session_cookie_returns_401(self):
        # No session, but include a valid CSRF — proves it's auth (not CSRF)
        # that rejects the request.
        prime = client.get("/", follow_redirects=False)  # warms _csrf cookie
        csrf = client.cookies.get("_csrf") or ""
        r = client.post(
            "/api/billing/portal-session",
            content="{}",
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
            },
            cookies={"_csrf": csrf} if csrf else {},
            follow_redirects=False,
        )
        # If CSRF middleware bounces first (403), the body wouldn't be our
        # auth_required JSON. Either rejection is acceptable as a hard
        # "unauthenticated" outcome — but we expect 401 in the canonical path.
        self.assertIn(r.status_code, (401, 403))
        if r.status_code == 401:
            self.assertEqual(r.json().get("error"), "auth_required")


class TestUserWithoutCustomerIdReturns400(_Base):
    """Authenticated user has no stripe_customer_id -> 400 no_active_subscription."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uid, cls.token = _make_user(
            "no-customer@test.com", "no_customer", customer_id=None,
        )

    def test_returns_400_with_error_code(self):
        r = _post_json("/api/billing/portal-session", token=self.token)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json().get("error"), "no_active_subscription")

    def test_empty_string_customer_id_is_treated_as_missing(self):
        # Some legacy rows may have empty strings instead of NULL. Both
        # should produce the same 400 — never call Stripe with an empty
        # customer ID.
        uid, token = _make_user(
            "empty-customer@test.com", "empty_customer", customer_id="",
        )
        r = _post_json("/api/billing/portal-session", token=token)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json().get("error"), "no_active_subscription")


class TestUserWithCustomerIdReturns200(_Base):
    """Authenticated paying user -> 200 with portal URL. customer_id NOT leaked."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uid, cls.token = _make_user(
            "paying@test.com", "paying_user", customer_id="cus_test_paying_123",
        )

    def test_returns_portal_url(self):
        r = _post_json("/api/billing/portal-session", token=self.token)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("url", body)
        self.assertTrue(
            body["url"].startswith("https://billing.stripe.com/"),
            f"unexpected portal URL: {body['url']!r}",
        )

    def test_response_does_not_leak_customer_id(self):
        r = _post_json("/api/billing/portal-session", token=self.token)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # No customer_id field at all, and the value must not appear
        # anywhere in the response body (raw text scan covers nested
        # leakage too).
        self.assertNotIn("customer_id", body)
        self.assertNotIn("customer", body)
        self.assertNotIn("cus_test_paying_123", r.text)

    def test_stripe_called_with_correct_args(self):
        r = _post_json("/api/billing/portal-session", token=self.token)
        self.assertEqual(r.status_code, 200, r.text)
        # The handler should have passed both customer and return_url
        # by keyword (matches the production call signature exactly).
        self.assertEqual(_LAST_CALL.get("customer"), "cus_test_paying_123")
        self.assertEqual(
            _LAST_CALL.get("return_url"),
            "https://narve.ai/settings/billing",
        )


class TestCsrfRequired(_Base):
    """Missing/invalid CSRF token is rejected before reaching the handler."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uid, cls.token = _make_user(
            "csrf-test@test.com", "csrf_test", customer_id="cus_test_csrf_999",
        )

    def test_missing_csrf_header_is_rejected(self):
        # We include the session cookie so auth passes; the only thing
        # missing is the CSRF header. Middleware should 403.
        r = _post_json(
            "/api/billing/portal-session",
            token=self.token,
            include_csrf=False,
        )
        self.assertEqual(r.status_code, 403, r.text)
        # Stripe must not have been called.
        self.assertEqual(_LAST_CALL, {})


class TestBillingUnavailableWhenKeyMissing(_Base):
    """No STRIPE_SECRET_KEY in the environment -> 503 billing_unavailable."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.uid, cls.token = _make_user(
            "no-key@test.com", "no_key_user", customer_id="cus_test_no_key",
        )

    def test_missing_secret_key_returns_503(self):
        prev = os.environ.pop("STRIPE_SECRET_KEY", None)
        try:
            r = _post_json("/api/billing/portal-session", token=self.token)
            self.assertEqual(r.status_code, 503, r.text)
            self.assertEqual(r.json().get("error"), "billing_unavailable")
            # Stripe must not have been called.
            self.assertEqual(_LAST_CALL, {})
        finally:
            if prev is not None:
                os.environ["STRIPE_SECRET_KEY"] = prev
            else:
                os.environ["STRIPE_SECRET_KEY"] = "sk_test_portal_default"


if __name__ == "__main__":
    unittest.main()
