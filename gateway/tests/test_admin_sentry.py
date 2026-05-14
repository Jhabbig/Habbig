"""Tests for the /admin/api/sentry recent-errors widget.

Covers:
  * Auth: anon + non-admin both 403.
  * No-config branch: returns ``error`` string, ``enabled=False`` when
    ``SENTRY_DSN`` is unset and no token is configured. Never reveals
    the auth token in the response body.
  * Happy path: with mocked httpx, the route parses Sentry's JSON into
    the documented shape (title/count/last_seen/level/permalink).
  * Caching: a second call within the TTL returns the cached payload
    without re-hitting httpx.
  * Refresh rate limit: 12 force-refresh calls succeed; the 13th
    falls back to the cache silently (no 429).
  * Token never leaks: response body must not contain the secret.

Network safety: every test patches ``httpx.AsyncClient.get`` so the
suite never reaches sentry.io. Failing this rule would make CI flaky
and burn the user's Sentry rate budget.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402
from observability import sentry_api as sapi  # noqa: E402


_SECRET_TOKEN = "sk_test_topsecret_AUTH_TOKEN_value_must_not_leak"


def _create_admin_session() -> str:
    email = f"sentry_admin_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.create_user(
            email, "Password1!verylong", username=f"sentry_admin_{os.getpid()}"
        )
    db.set_user_role(user_id, 2)
    try:
        db.set_user_2fa_method(user_id, "email_otp")
    except Exception:
        pass
    token = db.create_session(user_id)
    try:
        db.mark_session_two_fa_verified(token)
    except Exception:
        pass
    return token


def _create_regular_session() -> str:
    email = f"sentry_user_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = existing["id"]
        db.set_user_role(uid, 0)
    else:
        uid = db.create_user(
            email, "Password1!verylong", username=f"sentry_user_{os.getpid()}"
        )
        db.set_user_role(uid, 0)
    return db.create_session(uid)


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient — records calls + returns a canned
    response. Tests never touch the network."""

    instances: list["_FakeAsyncClient"] = []

    def __init__(self, *, response: _FakeResponse, **_kwargs):
        self._response = response
        self.get_calls: list[tuple[str, dict, dict]] = []
        _FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        self.get_calls.append((url, dict(headers or {}), dict(params or {})))
        return self._response


def _patch_httpx(payload, status_code=200):
    """Return a contextmanager that replaces httpx.AsyncClient with a fake.

    Resets the recorded-instances list so each test starts clean.
    """
    _FakeAsyncClient.instances.clear()
    response = _FakeResponse(status_code, payload)

    def factory(*args, **kwargs):
        return _FakeAsyncClient(response=response)

    return mock.patch("httpx.AsyncClient", factory)


class AdminSentryRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_cookies = {server.COOKIE_NAME: _create_admin_session()}
        cls.user_cookies = {server.COOKIE_NAME: _create_regular_session()}

    def setUp(self):
        sapi.invalidate_cache()
        # Reset refresh rate-limiter so test order doesn't matter.
        from admin_routes import _sentry_refresh_rate_limit
        _sentry_refresh_rate_limit.clear()
        # Clear any prior env leakage.
        for key in ("SENTRY_DSN", "SENTRY_AUTH_TOKEN", "SENTRY_ORG",
                    "SENTRY_PROJECT", "SENTRY_DASHBOARD_URL"):
            os.environ.pop(key, None)

    # ── Auth ────────────────────────────────────────────────────────────

    def test_anon_gets_403(self):
        r = self.client.get("/admin/api/sentry", cookies={})
        self.assertEqual(r.status_code, 403)

    def test_non_admin_gets_403(self):
        r = self.client.get("/admin/api/sentry", cookies=self.user_cookies)
        self.assertEqual(r.status_code, 403)

    # ── No-config branch ────────────────────────────────────────────────

    def test_no_config_returns_graceful_payload(self):
        r = self.client.get("/admin/api/sentry", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["count_24h"], 0)
        self.assertEqual(data["recent"], [])
        self.assertIn("SENTRY_AUTH_TOKEN", data["error"] or "")
        # Never leak the placeholder token even when env is unset.
        self.assertNotIn(_SECRET_TOKEN, json.dumps(data))

    # ── Happy path with mocked httpx ───────────────────────────────────

    def test_happy_path_parses_issues(self):
        os.environ["SENTRY_DSN"] = "https://abc@sentry.io/1"
        os.environ["SENTRY_AUTH_TOKEN"] = _SECRET_TOKEN
        os.environ["SENTRY_ORG"] = "narve"
        os.environ["SENTRY_PROJECT"] = "narve-backend"
        os.environ["SENTRY_DASHBOARD_URL"] = "https://sentry.io/organizations/narve/"

        issues = [
            {
                "id": "1001",
                "title": "ZeroDivisionError: division by zero",
                "culprit": "queries/markets.py",
                "lastSeen": "2026-05-14T18:00:00Z",
                "permalink": "https://sentry.io/organizations/narve/issues/1001/",
                "level": "error",
                "count": "42",
            },
            {
                "id": "1002",
                "title": "TimeoutError",
                "culprit": "subproducts/sports",
                "lastSeen": "2026-05-14T17:55:00Z",
                "permalink": "https://sentry.io/organizations/narve/issues/1002/",
                "level": "warning",
                "count": 3,
            },
        ]
        with _patch_httpx(issues, status_code=200):
            r = self.client.get("/admin/api/sentry", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["count_24h"], 2)
        self.assertEqual(len(data["recent"]), 2)
        first = data["recent"][0]
        self.assertEqual(first["title"], "ZeroDivisionError: division by zero")
        self.assertEqual(first["count"], 42)
        self.assertEqual(first["level"], "error")
        self.assertEqual(first["last_seen"], "2026-05-14T18:00:00Z")
        self.assertTrue(first["permalink"].startswith("https://"))
        self.assertEqual(
            data["dashboard_url"], "https://sentry.io/organizations/narve/"
        )

        # Token must NEVER appear in the response body.
        self.assertNotIn(_SECRET_TOKEN, json.dumps(data))
        # Request was made to the correct endpoint with the correct auth header.
        self.assertEqual(len(_FakeAsyncClient.instances), 1)
        call = _FakeAsyncClient.instances[0].get_calls[0]
        self.assertIn("sentry.io/api/0/projects/narve/narve-backend/issues/", call[0])
        self.assertEqual(call[1].get("Authorization"), f"Bearer {_SECRET_TOKEN}")

    # ── 5-minute cache ─────────────────────────────────────────────────

    def test_second_call_hits_cache(self):
        os.environ["SENTRY_DSN"] = "https://abc@sentry.io/1"
        os.environ["SENTRY_AUTH_TOKEN"] = _SECRET_TOKEN
        os.environ["SENTRY_ORG"] = "narve"
        os.environ["SENTRY_PROJECT"] = "narve-backend"

        issues = [{"id": "1", "title": "X", "lastSeen": "x", "permalink": "https://sentry.io/i/1/", "level": "error", "count": 1}]
        with _patch_httpx(issues, status_code=200):
            r1 = self.client.get("/admin/api/sentry", cookies=self.admin_cookies)
            r2 = self.client.get("/admin/api/sentry", cookies=self.admin_cookies)
            r3 = self.client.get("/admin/api/sentry", cookies=self.admin_cookies)

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r3.status_code, 200)
        # Three admin calls; only ONE upstream Sentry call.
        self.assertEqual(len(_FakeAsyncClient.instances), 1)
        self.assertEqual(len(_FakeAsyncClient.instances[0].get_calls), 1)

    # ── Refresh rate limit (12/hour per admin) ─────────────────────────

    def test_refresh_rate_limit_caps_at_twelve(self):
        os.environ["SENTRY_DSN"] = "https://abc@sentry.io/1"
        os.environ["SENTRY_AUTH_TOKEN"] = _SECRET_TOKEN
        os.environ["SENTRY_ORG"] = "narve"
        os.environ["SENTRY_PROJECT"] = "narve-backend"

        issues = [{"id": "1", "title": "X", "lastSeen": "x", "permalink": "https://sentry.io/i/1/", "level": "error", "count": 1}]
        with _patch_httpx(issues, status_code=200):
            # First call (no refresh) populates the cache: 1 upstream call.
            self.client.get("/admin/api/sentry", cookies=self.admin_cookies)
            # 12 force-refreshes are allowed: each one is an upstream call.
            for _ in range(12):
                r = self.client.get(
                    "/admin/api/sentry?refresh=1", cookies=self.admin_cookies
                )
                self.assertEqual(r.status_code, 200)
            # The 13th force-refresh trips the per-admin limiter and
            # silently falls back to the cached payload — no 429.
            r = self.client.get(
                "/admin/api/sentry?refresh=1", cookies=self.admin_cookies
            )
            self.assertEqual(r.status_code, 200)

        # 1 initial + 12 allowed refreshes = 13 upstream Sentry calls.
        total_get_calls = sum(len(c.get_calls) for c in _FakeAsyncClient.instances)
        self.assertEqual(total_get_calls, 13)

    # ── Sentry 5xx is graceful ─────────────────────────────────────────

    def test_sentry_5xx_returns_error_field(self):
        os.environ["SENTRY_DSN"] = "https://abc@sentry.io/1"
        os.environ["SENTRY_AUTH_TOKEN"] = _SECRET_TOKEN
        os.environ["SENTRY_ORG"] = "narve"
        os.environ["SENTRY_PROJECT"] = "narve-backend"

        with _patch_httpx({"detail": "down"}, status_code=503):
            r = self.client.get("/admin/api/sentry", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["count_24h"], 0)
        self.assertEqual(data["recent"], [])
        self.assertIn("503", data["error"] or "")
        self.assertNotIn(_SECRET_TOKEN, json.dumps(data))


class FetchSentrySummaryUnitTests(unittest.TestCase):
    """Direct unit tests on ``fetch_sentry_summary`` without going through
    the FastAPI layer. Catches regressions in the parser even if the route
    is later renamed."""

    def setUp(self):
        sapi.invalidate_cache()
        for key in ("SENTRY_DSN", "SENTRY_AUTH_TOKEN", "SENTRY_ORG",
                    "SENTRY_PROJECT", "SENTRY_DASHBOARD_URL"):
            os.environ.pop(key, None)

    def test_no_token_returns_error_field(self):
        import asyncio
        result = asyncio.run(sapi.fetch_sentry_summary())
        self.assertFalse(result["enabled"])
        self.assertEqual(result["recent"], [])
        self.assertIn("SENTRY_AUTH_TOKEN", result["error"] or "")

    def test_javascript_permalink_is_stripped(self):
        """A poisoned permalink scheme must not survive into the payload."""
        import asyncio
        os.environ["SENTRY_DSN"] = "https://x@sentry.io/1"
        os.environ["SENTRY_AUTH_TOKEN"] = _SECRET_TOKEN
        os.environ["SENTRY_ORG"] = "narve"
        os.environ["SENTRY_PROJECT"] = "narve-backend"
        os.environ["SENTRY_DASHBOARD_URL"] = "https://sentry.io/organizations/narve/"

        issues = [{
            "title": "Crash",
            "permalink": "javascript:alert(1)",
            "lastSeen": "2026-05-14T00:00:00Z",
            "level": "error",
            "count": 1,
        }]
        with _patch_httpx(issues, status_code=200):
            result = asyncio.run(sapi.fetch_sentry_summary())
        self.assertEqual(len(result["recent"]), 1)
        self.assertEqual(
            result["recent"][0]["permalink"],
            "https://sentry.io/organizations/narve/",
        )


if __name__ == "__main__":
    unittest.main()
