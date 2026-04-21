"""HTTP tests for the post-token first-run experience.

Wires onboarding_routes.register(app) into a fresh FastAPI TestClient,
patches server.current_user / _require_admin_user so we don't need the
full auth stack, and exercises every route in the 5-step flow + the
first-week-goals widget + the admin metrics page.

Migration 090 + 091 must apply cleanly for the tests to pass — we apply
them in setUpClass against a temp file-backed DB so the routes see the
tables.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Pull _testdb first to keep the shared in-memory DB consistent across files.
from tests import _testdb  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import migrations
import server as _server


def _fresh_db() -> Path:
    p = Path(tempfile.mktemp(suffix=".db", prefix="narve-onboarding-test-"))
    os.environ["GATEWAY_DB_PATH"] = str(p)
    conn = sqlite3.connect(p)
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
    return p


class TestOnboardingRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = _fresh_db()

        # Seed a user directly — we skip the auth flow entirely.
        now = int(time.time())
        conn = sqlite3.connect(cls.db_path)
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, password_salt, created_at) "
            "VALUES (?, ?, ?, 'x', 'x', ?)",
            (501, "julian.test", "julian.test@narve.ai", now),
        )
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, password_salt, "
            "created_at, is_admin) VALUES (?, ?, ?, 'x', 'x', ?, 1)",
            (502, "admin.test", "admin.test@narve.ai", now),
        )
        conn.commit()
        conn.close()

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import onboarding_routes

        cls.app = FastAPI()
        onboarding_routes.register(cls.app)

        cls._orig_current_user = _server.current_user
        cls._orig_require_admin = _server._require_admin_user

        def fake_current_user(request):
            return getattr(request.state, "_test_user", None)

        def fake_require_admin(request, page=False):
            user = getattr(request.state, "_test_user", None)
            if user and user.get("is_admin"):
                return user
            if page:
                return None
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Admin required")

        def fake_render_page(name, request=None, **context):
            # Skip the real template loader — the route only cares about the
            # context fields the test then reads via the return body.
            from fastapi.responses import HTMLResponse
            kv = " ".join(f"{k}={context.get(k, '')}" for k in
                          ("email", "username", "first_name"))
            return HTMLResponse(f"<html><body>name={name} {kv}</body></html>")

        _server.current_user = fake_current_user
        _server._require_admin_user = fake_require_admin
        cls._orig_render = _server.render_page
        _server.render_page = fake_render_page

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

        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        _server.current_user = cls._orig_current_user
        _server._require_admin_user = cls._orig_require_admin
        _server.render_page = cls._orig_render
        try:
            cls.db_path.unlink(missing_ok=True)
        except Exception:
            pass

    def setUp(self):
        # Wipe per-user state between tests so goal counts don't leak.
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM user_onboarding")
        conn.execute("DELETE FROM user_first_week_goals")
        conn.commit()
        conn.close()

    # ── Helpers ────────────────────────────────────────────────────────

    def _user(self, *, user_id=501, email="julian.test@narve.ai", is_admin=False):
        return {"x-test-user-json": json.dumps({
            "user_id": user_id, "email": email,
            "username": email.split("@")[0], "is_admin": is_admin,
        })}

    def _admin(self):
        return self._user(user_id=502, email="admin.test@narve.ai", is_admin=True)

    # ── Migrations ─────────────────────────────────────────────────────

    def test_migrations_applied(self):
        conn = sqlite3.connect(self.db_path)
        tbls = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for name in ("user_onboarding", "user_first_week_goals"):
            self.assertIn(name, tbls)
        conn.close()

    # ── Flow ───────────────────────────────────────────────────────────

    def test_onboarding_page_requires_auth(self):
        r = self.client.get("/onboarding")
        self.assertEqual(r.status_code, 401)

    def test_onboarding_page_renders_with_first_name(self):
        r = self.client.get("/onboarding", headers=self._user())
        self.assertEqual(r.status_code, 200)
        self.assertIn("first_name=Julian", r.text)

    def test_advance_step_updates_row(self):
        r = self.client.post("/api/onboarding/advance",
                             headers=self._user(), data={"step": 2})
        self.assertEqual(r.status_code, 200)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT step_completed FROM user_onboarding WHERE user_id = 501"
        ).fetchone()
        conn.close()
        self.assertEqual(row["step_completed"], 2)

    def test_dismiss_tour_stamps_completed_and_dismissed(self):
        r = self.client.post("/api/onboarding/dismiss", headers=self._user())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["dismissed"])
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT dismissed, completed_at FROM user_onboarding WHERE user_id = 501"
        ).fetchone()
        conn.close()
        self.assertEqual(row["dismissed"], 1)
        self.assertIsNotNone(row["completed_at"])

    def test_categories_rejects_invalid_and_caps_at_3(self):
        r = self.client.post(
            "/api/onboarding/categories",
            headers=self._user(),
            data={"categories": "crypto,politics,sports,weather,bogus"},
        )
        self.assertEqual(r.status_code, 200)
        picked = r.json()["categories"]
        self.assertEqual(len(picked), 3)
        for c in picked:
            self.assertIn(c, ("crypto", "politics", "sports", "weather"))

    def test_follow_sources_marks_first_week_goal_at_three(self):
        # 2 handles → goal NOT triggered
        r = self.client.post(
            "/api/onboarding/follow-sources",
            headers=self._user(), data={"handles": "a,b"},
        )
        self.assertFalse(r.json()["goal_triggered"])

        # 3 handles → goal triggered
        r = self.client.post(
            "/api/onboarding/follow-sources",
            headers=self._user(), data={"handles": "a,b,c"},
        )
        self.assertTrue(r.json()["goal_triggered"])
        # Verify a row landed.
        goals = self.client.get("/api/first-week/goals", headers=self._user()).json()
        completed = [g for g in goals["goals"] if g["completed"]]
        self.assertTrue(any(g["key"] == "follow_3_sources" for g in completed))

    def test_notifications_enabled_marks_goal(self):
        r = self.client.post(
            "/api/onboarding/notifications", headers=self._user(), data={"enabled": 1},
        )
        self.assertTrue(r.json()["goal_triggered"])
        goals = self.client.get("/api/first-week/goals", headers=self._user()).json()
        keys_done = {g["key"] for g in goals["goals"] if g["completed"]}
        self.assertIn("enable_notifications", keys_done)

    def test_complete_sets_completed_at(self):
        r = self.client.post("/api/onboarding/complete", headers=self._user())
        self.assertTrue(r.json()["completed"])
        self.assertEqual(r.json()["redirect"], "/dashboard?first_visit=1")

    # ── Suggested sources + sample signal ─────────────────────────────

    def test_suggested_sources_handles_empty_db(self):
        r = self.client.get(
            "/api/onboarding/suggested-sources?categories=crypto,finance",
            headers=self._user(),
        )
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json()["sources"], list)

    def test_sample_signal_returns_narrative_when_no_data(self):
        r = self.client.get("/api/onboarding/sample-signal", headers=self._user())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("narrative", body)

    # ── Sample feed ───────────────────────────────────────────────────

    def test_sample_feed_always_returns_five(self):
        r = self.client.get("/api/feed/sample", headers=self._user())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["sample"])
        self.assertEqual(len(body["predictions"]), 5)

    # ── Goals widget ──────────────────────────────────────────────────

    def test_goals_state_defaults_to_unhide(self):
        r = self.client.get("/api/first-week/goals", headers=self._user())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["hide_widget"])
        self.assertEqual(body["completed_count"], 0)
        self.assertEqual(body["total"], 6)

    def test_mark_goal_via_post(self):
        r = self.client.post(
            "/api/first-week/goals/view_1_market_detail",
            headers=self._user(),
        )
        self.assertEqual(r.status_code, 200)
        state = self.client.get("/api/first-week/goals", headers=self._user()).json()
        keys_done = {g["key"] for g in state["goals"] if g["completed"]}
        self.assertIn("view_1_market_detail", keys_done)

    def test_mark_unknown_goal_400s(self):
        r = self.client.post(
            "/api/first-week/goals/not_a_goal",
            headers=self._user(),
        )
        self.assertEqual(r.status_code, 400)

    def test_widget_dismiss_hides_going_forward(self):
        self.client.post("/api/first-week/widget/dismiss", headers=self._user())
        state = self.client.get("/api/first-week/goals", headers=self._user()).json()
        self.assertTrue(state["dismissed"])
        self.assertTrue(state["hide_widget"])

    # ── Admin metrics ─────────────────────────────────────────────────

    def test_admin_metrics_requires_admin(self):
        r = self.client.get("/admin/api/onboarding/metrics", headers=self._user())
        self.assertEqual(r.status_code, 403)

    def test_admin_metrics_returns_json(self):
        # Seed one completed flow.
        self.client.post("/api/onboarding/complete", headers=self._user())
        r = self.client.get("/admin/api/onboarding/metrics", headers=self._admin())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body["onboarding_completed"], 1)
        self.assertGreaterEqual(body["total_users"], 1)

    def test_admin_metrics_page_renders(self):
        r = self.client.get("/admin/onboarding", headers=self._admin())
        self.assertEqual(r.status_code, 200)
        self.assertIn("Onboarding", r.text)
        self.assertIn("First-week goals", r.text)

    def test_admin_metrics_page_denies_non_admin(self):
        # _require_admin_user returns None with page=True → route returns
        # the explicit 403 raised by our fake.
        r = self.client.get("/admin/onboarding", headers=self._user())
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
