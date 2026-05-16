"""Tests for /admin/health-monitor + /api/admin/health-monitor.

These cover:
  - non-admin callers get 403 on both the page and the API
  - admin callers get 200 and the page rendered with the expected scaffold
  - the API returns the expected ``{services: [...], count, generated_at}`` shape
  - when a service's probe raises (or returns 5xx), it is reported as ``down``

Auth setup mirrors ``test_log_admin.py``: seed a real admin user + session
in the SQLite DB and mark it 2FA-verified so ``_require_admin_user``
lets it through.
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
import admin_health_monitor_routes as ahm  # noqa: E402


def _create_admin_session() -> str:
    email = f"hm_admin_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.create_user(email, "Password1!verylong", username=f"hm_admin_{os.getpid()}")
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
    email = f"hm_user_{os.getpid()}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = existing["id"]
        db.set_user_role(uid, 0)
    else:
        uid = db.create_user(email, "Password1!verylong", username=f"hm_user_{os.getpid()}")
        db.set_user_role(uid, 0)
    return db.create_session(uid)


def _reset_cache():
    """Clear the 5s response cache and the 24h uptime ring."""
    with ahm._cache_lock:
        ahm._cache["payload"] = None
        ahm._cache["expires_at"] = 0.0
    with ahm._ring_lock:
        ahm._ring.clear()


class AdminHealthMonitorTestCase(unittest.TestCase):
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
        _reset_cache()

    # Auth

    def test_page_requires_admin_anon(self):
        r = self.client.get("/admin/health-monitor", cookies={}, follow_redirects=False)
        # Anonymous callers are punted to /gate by _denied_response.
        self.assertIn(r.status_code, (302, 303, 403))

    def test_page_rejects_non_admin(self):
        r = self.client.get(
            "/admin/health-monitor",
            cookies=self.user_cookies,
            follow_redirects=False,
        )
        # Non-admin logged-in users hit the 403 page from _denied_response.
        self.assertEqual(r.status_code, 403)

    def test_api_rejects_anon(self):
        r = self.client.get("/api/admin/health-monitor", cookies={})
        self.assertEqual(r.status_code, 403)

    def test_api_rejects_non_admin(self):
        r = self.client.get("/api/admin/health-monitor", cookies=self.user_cookies)
        self.assertEqual(r.status_code, 403)

    # Page renders

    def test_page_admin_200(self):
        with mock.patch.object(ahm, "_probe_all", return_value={
            "services": [
                {**svc, "status": "up", "latency_ms": 12,
                 "last_check": 1700000000, "uptime_24h": 100.0}
                for svc in ahm.SERVICES
            ],
            "count": len(ahm.SERVICES),
            "generated_at": 1700000000,
        }):
            r = self.client.get(
                "/admin/health-monitor",
                cookies=self.admin_cookies,
                follow_redirects=False,
            )
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertIn("Health monitor", body)
        self.assertIn("hm-grid", body)
        self.assertIn("/api/admin/health-monitor", body)

    # API shape

    def test_api_returns_expected_shape(self):
        def fake_probe(svc, client):
            return {
                "name": svc["name"],
                "slug": svc["slug"],
                "port": svc["port"],
                "status": "up",
                "latency_ms": 7,
                "last_check": 1700000000,
                "uptime_24h": 100.0,
            }
        with mock.patch.object(ahm, "_probe", side_effect=fake_probe):
            r = self.client.get("/api/admin/health-monitor", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("services", data)
        self.assertIn("count", data)
        self.assertIn("generated_at", data)
        self.assertEqual(data["count"], len(ahm.SERVICES))
        self.assertEqual(len(data["services"]), len(ahm.SERVICES))

        for entry in data["services"]:
            for key in ("name", "slug", "port", "status", "latency_ms", "last_check"):
                self.assertIn(key, entry, f"missing {key} in {entry}")
            self.assertIn(entry["status"], ("up", "slow", "down"))

        names = {e["name"] for e in data["services"]}
        # Display names mirror config.json display_name (see SERVICES in
        # admin_health_monitor_routes.py). "Top Traders" → "Traders" and
        # "World Health" → "Health" was synced in commit 176c613.
        for expected in [
            "Gateway", "Sports", "Weather", "World", "Crypto", "Midterm",
            "Traders", "Voters", "Climate", "Disasters", "Whale",
            "Central Bank", "Health", "Love",
        ]:
            self.assertIn(expected, names, f"{expected} missing from API response")

    # Mocked down scenario

    def test_service_down_reports_down(self):
        """Probe that raises on connection -> status=down."""
        import httpx

        class _BoomClient:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def head(self, *_args, **_kw):
                raise httpx.ConnectError("connection refused")

        with mock.patch.object(ahm.httpx, "Client", _BoomClient):
            r = self.client.get("/api/admin/health-monitor", cookies=self.admin_cookies)

        self.assertEqual(r.status_code, 200)
        data = r.json()
        statuses = {e["status"] for e in data["services"]}
        self.assertEqual(statuses, {"down"})
        for entry in data["services"]:
            self.assertEqual(entry["status"], "down")
            self.assertIn("latency_ms", entry)


if __name__ == "__main__":
    unittest.main()
