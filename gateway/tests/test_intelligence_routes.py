"""HTTP/integration tests for the intelligence-layer route files.

Each new *_routes.py file exposes a ``register(app)`` function. These
tests spin up a fresh FastAPI app, call register, and exercise the
handlers end-to-end — no server.py involvement required. Gives us
coverage today without waiting for server.py wiring.

Auth + subscription helpers are monkey-patched on the ``server`` module
so the Pro-gating routes don't need a full fixture user. We only swap
``current_user`` and ``_user_plan_info`` — nothing else in server.py is
touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Pull in the shared in-memory DB fixture before anything else imports
# server / ai modules — otherwise they'd open the real auth.db.
from tests import _testdb  # noqa: F401


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import migrations  # noqa: E402
import server as _server  # noqa: E402


def _tmp_db_path() -> Path:
    """Path the feature modules will hit via GATEWAY_DB_PATH. We copy
    the shared _testdb contents into a physical file for each test run
    because the feature modules open their own sqlite3 connections
    against a file path (they can't share the in-memory handle).
    """
    # One temp DB per import — reused across tests in this module.
    return Path(_testdb._conn.execute("PRAGMA database_list").fetchone()["file"] or ":memory:")


def _reset_physical_db() -> Path:
    """Drop a temp file-backed DB, apply migrations, return the path."""
    import tempfile
    tmp = Path(tempfile.mktemp(suffix=".db", prefix="narve-routes-test-"))
    os.environ["GATEWAY_DB_PATH"] = str(tmp)
    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    orig = db.conn

    @contextlib.contextmanager
    def fake():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    db.conn = fake
    try:
        db.init_db()
        migrations.upgrade_to_head()
    finally:
        db.conn = orig
    conn.close()
    return tmp


class _RouteTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = _reset_physical_db()
        # Import fastapi + TestClient lazily so tests that don't run
        # this class don't pay the import cost.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        cls.app = FastAPI()
        cls.register_routes(cls.app)
        cls.client = TestClient(cls.app)

        # Patch auth / plan helpers on server.
        cls._orig_current_user = _server.current_user
        cls._orig_plan_info = _server._user_plan_info
        cls._orig_require_admin = _server._require_admin_user

        def fake_current_user(request):
            return getattr(request.state, "_test_user", None)

        def fake_plan_info(user, subs, now_ts):
            plan = (user or {}).get("_test_plan") or "none"
            return {"plan": plan, "label": plan.title(), "credits": 6,
                    "monthly": 0, "annual": 0}

        def fake_require_admin(request, page=False):
            user = getattr(request.state, "_test_user", None)
            if user and user.get("is_admin"):
                return user
            if page:
                return None
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="admin required")

        _server.current_user = fake_current_user
        _server._user_plan_info = fake_plan_info
        _server._require_admin_user = fake_require_admin

        # Middleware that stuffs the test user onto request.state.
        @cls.app.middleware("http")
        async def _set_user(request, call_next):
            header = request.headers.get("x-test-user-json")
            if header:
                try:
                    request.state._test_user = json.loads(header)
                except Exception:
                    request.state._test_user = None
            else:
                request.state._test_user = None
            return await call_next(request)

    @classmethod
    def tearDownClass(cls):
        _server.current_user = cls._orig_current_user
        _server._user_plan_info = cls._orig_plan_info
        _server._require_admin_user = cls._orig_require_admin
        try:
            cls.db_path.unlink(missing_ok=True)
        except Exception:
            pass

    @classmethod
    def register_routes(cls, app) -> None:
        raise NotImplementedError

    # ── Helpers ─────────────────────────────────────────────────────────

    def _user_header(self, *, user_id: int = 1, email: str = "test@narve.ai",
                     plan: str = "pro", is_admin: bool = False) -> dict:
        return {"x-test-user-json": json.dumps({
            "user_id": user_id, "email": email,
            "is_admin": is_admin, "_test_plan": plan,
        })}


# ── Backtest routes ─────────────────────────────────────────────────────


class TestBacktestRoutes(_RouteTestBase):
    @classmethod
    def register_routes(cls, app):
        import backtest_routes
        backtest_routes.register(app)

    def test_unauthenticated_runs_list_401(self):
        r = self.client.get("/api/backtest/runs")
        self.assertEqual(r.status_code, 401)

    def test_non_pro_user_402(self):
        r = self.client.get("/api/backtest/runs", headers=self._user_header(plan="none"))
        self.assertEqual(r.status_code, 402)

    def test_pro_user_sees_runs_list(self):
        # Other tests in this class may have inserted rows; we just
        # verify the shape + accessibility rather than emptiness.
        r = self.client.get("/api/backtest/runs", headers=self._user_header(plan="pro"))
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json().get("runs"), list)

    def test_create_run_returns_id(self):
        params = {"bet_sizing": "flat", "flat_bet_size": 100, "starting_bankroll": 1000}
        r = self.client.post(
            "/api/backtest/runs",
            headers=self._user_header(plan="pro"),
            data={"name": "smoke", "params": json.dumps(params)},
        )
        self.assertEqual(r.status_code, 202)
        self.assertIn("run_id", r.json())


# ── Network routes ──────────────────────────────────────────────────────


class TestNetworkRoutes(_RouteTestBase):
    @classmethod
    def register_routes(cls, app):
        import network_routes
        network_routes.register(app)

    def test_network_html_renders_empty(self):
        r = self.client.get("/sources/network")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Source network", r.text)
        self.assertIn("No echo-chamber clusters", r.text)

    def test_network_json_empty_snapshot(self):
        r = self.client.get("/api/sources/network")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["snapshot"])


# ── Alerts routes ───────────────────────────────────────────────────────


class TestAlertsRoutes(_RouteTestBase):
    @classmethod
    def register_routes(cls, app):
        import alerts_routes
        alerts_routes.register(app)

    def test_create_and_list_rule(self):
        headers = self._user_header(user_id=42, email="x@narve.ai")
        r = self.client.post(
            "/api/alerts", headers=headers,
            data={"alert_type": "odds_movement", "min_movement_pct": "0.10"},
        )
        self.assertEqual(r.status_code, 201)
        rule_id = r.json()["id"]
        r2 = self.client.get("/api/alerts", headers=headers)
        self.assertEqual(r2.status_code, 200)
        rules = r2.json()["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["id"], rule_id)

    def test_delete_rule(self):
        headers = self._user_header(user_id=43)
        r = self.client.post(
            "/api/alerts", headers=headers,
            data={"alert_type": "volume_spike"},
        )
        rule_id = r.json()["id"]
        r2 = self.client.delete(f"/api/alerts/{rule_id}", headers=headers)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["deleted"])

    def test_rate_limit_10_per_user(self):
        headers = self._user_header(user_id=44)
        for i in range(10):
            r = self.client.post(
                "/api/alerts", headers=headers,
                data={"alert_type": "odds_movement"},
            )
            self.assertEqual(r.status_code, 201)
        r = self.client.post(
            "/api/alerts", headers=headers,
            data={"alert_type": "volume_spike"},
        )
        self.assertEqual(r.status_code, 429)


# ── Insider routes ──────────────────────────────────────────────────────


class TestInsiderRoutes(_RouteTestBase):
    @classmethod
    def register_routes(cls, app):
        import insider_routes
        insider_routes.register(app)

    def test_requires_pro(self):
        r = self.client.get("/api/insider/signals", headers=self._user_header(plan="none"))
        self.assertEqual(r.status_code, 402)

    def test_signals_empty_returns_disclaimer(self):
        r = self.client.get("/api/insider/signals",
                            headers=self._user_header(plan="pro"))
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertEqual(payload["signals"], [])
        self.assertIn("disclosures", payload["disclaimer"].lower())


# ── AI routes ───────────────────────────────────────────────────────────


class TestAiRoutes(_RouteTestBase):
    @classmethod
    def register_routes(cls, app):
        import ai_routes
        ai_routes.register(app)

    def test_source_summary_fallback_for_unknown(self):
        r = self.client.get("/api/sources/neverseen/summary")
        self.assertEqual(r.status_code, 200)
        self.assertIn("not yet made enough", r.json()["summary"])

    def test_admin_usage_requires_admin(self):
        r = self.client.get("/admin/api/ai/usage")
        self.assertEqual(r.status_code, 403)

    def test_admin_usage_ok_for_admin(self):
        r = self.client.get(
            "/admin/api/ai/usage",
            headers=self._user_header(is_admin=True),
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("rollup", r.json())

    def test_admin_ai_usage_html_requires_admin(self):
        r = self.client.get("/admin/ai-usage", headers=self._user_header(plan="pro"))
        self.assertEqual(r.status_code, 403)

    def test_admin_ai_usage_html_renders_for_admin(self):
        r = self.client.get("/admin/ai-usage", headers=self._user_header(is_admin=True))
        self.assertEqual(r.status_code, 200)
        self.assertIn("AI usage", r.text)
        self.assertIn("Cache hit rate", r.text)


# ── Reports routes ──────────────────────────────────────────────────────


class TestReportsRoutes(_RouteTestBase):
    @classmethod
    def register_routes(cls, app):
        import reports_routes
        reports_routes.register(app)

    def test_requires_pro(self):
        r = self.client.get("/reports/weekly",
                            headers=self._user_header(plan="none"))
        self.assertEqual(r.status_code, 402)

    def test_pro_sees_empty_list(self):
        r = self.client.get("/reports/weekly",
                            headers=self._user_header(plan="pro"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Weekly reports", r.text)


# ── Environmental routes ────────────────────────────────────────────────


class TestEnvironmentalRoutes(_RouteTestBase):
    @classmethod
    def register_routes(cls, app):
        import environmental_routes
        environmental_routes.register(app)

    def test_top_env_returns_empty(self):
        r = self.client.get("/api/markets/environmental/top",
                            headers=self._user_header(plan="pro"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["markets"], [])

    def test_update_preferences_requires_unit_in_whitelist(self):
        r = self.client.patch(
            "/api/user/preferences/environmental",
            headers=self._user_header(plan="pro"),
            data={"preferred_unit": "made_up"},
        )
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
