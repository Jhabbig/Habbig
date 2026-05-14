"""Tests for /admin/integrations + /api/admin/integrations*.

Covers:
  - anonymous and non-admin callers get 403 (page + API + test endpoint)
  - admin callers get 200 with all 8 integrations represented
  - get_integration_status returns the 8-key dict shape
  - "Test connection" works (mocked) for Anthropic and Polymarket

Auth setup mirrors test_admin_health_monitor.py / test_admin_jobs.py:
seed a real admin user + session in the SQLite DB and mark it 2FA-verified
so ``_require_admin_user`` lets it through.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402
import admin_integrations_routes as ai_routes  # noqa: E402
from queries import integrations as integrations_q  # noqa: E402


SLUGS = (
    "stripe", "anthropic", "polymarket", "kalshi",
    "smtp", "sentry", "betterstack", "cloudflare",
)


_CSRF_TOKEN = "test-csrf-token-integrations-suite"


def _csrf_headers() -> dict:
    return {server.CSRF_HEADER_NAME: _CSRF_TOKEN}


def _create_admin_session() -> str:
    email = f"int_admin_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.create_user(
            email, "Password1!verylong",
            username=f"int_admin_{os.getpid()}",
        )
    db.set_user_role(user_id, 2)  # super admin
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
    email = f"int_user_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = existing["id"]
        db.set_user_role(uid, 0)
    else:
        uid = db.create_user(
            email, "Password1!verylong",
            username=f"int_user_{os.getpid()}",
        )
        db.set_user_role(uid, 0)
    return db.create_session(uid)


class AdminIntegrationsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import migrations as _migrations
            _migrations.upgrade_to_head()
        except Exception:
            pass
        cls.client = TestClient(server.app)
        cls.admin_cookies = {
            server.COOKIE_NAME: _create_admin_session(),
            server.CSRF_COOKIE_NAME: _CSRF_TOKEN,
        }
        cls.user_cookies = {server.COOKIE_NAME: _create_regular_session()}

    # ── Auth ─────────────────────────────────────────────────────────

    def test_page_rejects_anon(self):
        r = self.client.get(
            "/admin/integrations", cookies={}, follow_redirects=False,
        )
        # Anonymous callers are punted to /gate by _denied_response.
        self.assertIn(r.status_code, (302, 303, 403))

    def test_page_rejects_non_admin(self):
        r = self.client.get(
            "/admin/integrations",
            cookies=self.user_cookies, follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    def test_api_rejects_anon(self):
        r = self.client.get("/api/admin/integrations", cookies={})
        self.assertEqual(r.status_code, 403)

    def test_api_rejects_non_admin(self):
        r = self.client.get(
            "/api/admin/integrations", cookies=self.user_cookies,
        )
        self.assertEqual(r.status_code, 403)

    def test_test_endpoint_rejects_anon(self):
        r = self.client.post(
            "/api/admin/integrations/anthropic/test", cookies={},
        )
        self.assertIn(r.status_code, (403, 401, 400))

    # ── Page renders ─────────────────────────────────────────────────

    def test_page_admin_200(self):
        r = self.client.get(
            "/admin/integrations",
            cookies=self.admin_cookies, follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.text
        # Page chrome
        self.assertIn("Integrations", body)
        self.assertIn("Connected", body)
        self.assertIn("Degraded", body)
        self.assertIn("Down", body)
        # All 8 integrations must be present in the rendered HTML.
        self.assertIn("Stripe", body)
        self.assertIn("Anthropic", body)
        self.assertIn("Polymarket", body)
        self.assertIn("Kalshi", body)
        self.assertIn("SMTP", body)
        self.assertIn("Sentry", body)
        self.assertIn("BetterStack", body)
        self.assertIn("Cloudflare", body)
        # Glyphs present (any of solid/hollow/×).
        self.assertTrue(
            any(g in body for g in ("●", "○", "×")),
            "expected at least one status glyph",
        )

    # ── Query module shape ───────────────────────────────────────────

    def test_get_integration_status_returns_eight(self):
        snapshot = integrations_q.get_integration_status()
        self.assertEqual(set(snapshot.keys()), set(SLUGS))
        for slug, row in snapshot.items():
            self.assertEqual(row["slug"], slug)
            self.assertIn(row["status"], (
                integrations_q.STATUS_CONNECTED,
                integrations_q.STATUS_DEGRADED,
                integrations_q.STATUS_DOWN,
            ))
            for key in ("name", "summary", "details", "testable"):
                self.assertIn(key, row, f"{slug} missing {key}")

    def test_api_returns_all_eight(self):
        r = self.client.get(
            "/api/admin/integrations", cookies=self.admin_cookies,
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("integrations", data)
        self.assertEqual(data["count"], 8)
        self.assertEqual(set(data["integrations"].keys()), set(SLUGS))

    # ── Test connection — Anthropic + Polymarket (mocked) ────────────

    def test_test_anthropic_ok(self):
        """Patch the async client to return a stub message — endpoint
        should report ok=True + a non-negative latency."""

        class _StubMessages:
            async def create(self, **_kw):
                class R:
                    id = "msg_test"
                return R()

        class _StubSDK:
            messages = _StubMessages()

        env_patch = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"})
        with env_patch, mock.patch(
            "ai.client.get_async_client", return_value=_StubSDK()
        ):
            r = self.client.post(
                "/api/admin/integrations/anthropic/test",
                cookies=self.admin_cookies,
                headers=_csrf_headers(),
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("ok"), msg=f"expected ok=True, got {body!r}")
        self.assertIn("latency_ms", body)
        self.assertGreaterEqual(int(body["latency_ms"]), 0)

    def test_test_anthropic_missing_key(self):
        """No env key — endpoint returns ok=False with a clear error."""
        env_patch = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""})
        with env_patch:
            r = self.client.post(
                "/api/admin/integrations/anthropic/test",
                cookies=self.admin_cookies,
                headers=_csrf_headers(),
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body.get("ok"))
        self.assertIn("ANTHROPIC_API_KEY", body.get("error", ""))

    def test_test_polymarket_ok(self):
        """Mock the httpx HEAD call to return 200 — endpoint reports ok."""

        class _Resp:
            def __init__(self, code):
                self.status_code = code

        async def _fake_head(self, url, *args, **kw):
            return _Resp(200)

        with mock.patch.object(
            ai_routes.httpx.AsyncClient, "head", _fake_head
        ):
            r = self.client.post(
                "/api/admin/integrations/polymarket/test",
                cookies=self.admin_cookies,
                headers=_csrf_headers(),
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("ok"))
        self.assertIn("latency_ms", body)

    def test_test_polymarket_failure(self):
        """HEAD returns 500 — endpoint reports ok=False."""

        class _Resp:
            def __init__(self, code):
                self.status_code = code

        async def _fake_head(self, url, *args, **kw):
            return _Resp(503)

        with mock.patch.object(
            ai_routes.httpx.AsyncClient, "head", _fake_head
        ):
            r = self.client.post(
                "/api/admin/integrations/polymarket/test",
                cookies=self.admin_cookies,
                headers=_csrf_headers(),
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body.get("ok"))
        self.assertIn("503", body.get("error", ""))

    def test_test_unknown_slug_400(self):
        r = self.client.post(
            "/api/admin/integrations/notreal/test",
            cookies=self.admin_cookies,
            headers=_csrf_headers(),
        )
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
