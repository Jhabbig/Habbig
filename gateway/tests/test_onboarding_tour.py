"""Tests for the dashboard overlay tour + first-week goals widget gating.

Covers:
  * Migration 171 — tour_completed_at + tour_skipped + tour_skipped_at on
    user_onboarding (idempotent on re-apply).
  * GET /api/onboarding/tour-state — should_show iff:
      - user finished the 5-step flow (completed_at IS NOT NULL),
      - tour not yet completed AND not skipped.
  * POST /api/onboarding/tour-complete — stamps tour_completed_at,
    idempotent on repeat (first ts wins via COALESCE).
  * POST /api/onboarding/tour-skip — sets tour_skipped=1 + tour_skipped_at.
  * Skipping or completing the tour flips should_show to False.
  * GET /api/first-week/goals (and the /api/onboarding/goals alias) —
    hide_widget=true once all goals complete OR after explicit dismiss.

Uses an on-disk temp DB so onboarding_routes._connect() (which opens its
own sqlite3.connect()) and our setup writes (via sqlite3.connect on the
same path) talk to the same tables.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Pull _testdb first so other suites running concurrently don't trip our
# db.conn rebind. We then point GATEWAY_DB_PATH at a fresh on-disk file
# so onboarding_routes._connect() (bypasses db.conn entirely) sees the
# same schema we set up here.
from tests import _testdb  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import migrations  # noqa: E402
import server as _server  # noqa: E402


def _fresh_db() -> Path:
    """Materialise a temp on-disk DB with all migrations applied."""
    p = Path(tempfile.mktemp(suffix=".db", prefix="narve-onboarding-tour-"))
    os.environ["GATEWAY_DB_PATH"] = str(p)

    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    orig_conn = db.conn

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
        db.conn = orig_conn
    conn.close()
    return p


class _RouteFixture:
    """Spin up an isolated FastAPI app + onboarding routes, with the
    auth helpers stubbed to read the test user off request.state."""

    @classmethod
    def boot(cls, db_path: Path) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import onboarding_routes

        cls.app = FastAPI()
        onboarding_routes.register(cls.app)

        cls._orig_current_user = _server.current_user

        def fake_current_user(request):
            return getattr(request.state, "_test_user", None)

        _server.current_user = fake_current_user

        @cls.app.middleware("http")
        async def _set_user(request, call_next):
            header = request.headers.get("x-test-user-id")
            if header:
                request.state._test_user = {
                    "user_id": int(header),
                    "email": f"u{header}@test.local",
                    "is_admin": header == "999",
                }
            return await call_next(request)

        cls.client = TestClient(cls.app)
        cls.db_path = db_path

    @classmethod
    def teardown(cls) -> None:
        _server.current_user = cls._orig_current_user
        try:
            os.unlink(cls.db_path)
        except OSError:
            pass

    @classmethod
    def as_user(cls, uid: int) -> dict:
        return {"x-test-user-id": str(uid)}


# ── Direct DB helpers (operate on the on-disk temp file) ──────────────────


def _conn_test(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


_USER_COUNTER = {"n": 1000}


def _make_user(db_path: Path) -> int:
    _USER_COUNTER["n"] += 1
    uid = _USER_COUNTER["n"]
    now = int(time.time())
    c = _conn_test(db_path)
    try:
        c.execute(
            "INSERT INTO users (id, username, email, password_hash, password_salt, created_at) "
            "VALUES (?, ?, ?, 'x', 'x', ?)",
            (uid, f"tour{uid}", f"tour{uid}@test.local", now),
        )
        c.commit()
    finally:
        c.close()
    return uid


def _ensure_onboarding_row(db_path: Path, uid: int) -> None:
    c = _conn_test(db_path)
    try:
        c.execute(
            "INSERT OR IGNORE INTO user_onboarding (user_id, started_at) "
            "VALUES (?, ?)",
            (uid, int(time.time())),
        )
        c.commit()
    finally:
        c.close()


def _mark_flow_complete(db_path: Path, uid: int) -> None:
    _ensure_onboarding_row(db_path, uid)
    c = _conn_test(db_path)
    try:
        c.execute(
            "UPDATE user_onboarding SET completed_at = ? WHERE user_id = ?",
            (int(time.time()), uid),
        )
        c.commit()
    finally:
        c.close()


def _read_onboarding(db_path: Path, uid: int) -> sqlite3.Row:
    c = _conn_test(db_path)
    try:
        return c.execute(
            "SELECT * FROM user_onboarding WHERE user_id = ?", (uid,),
        ).fetchone()
    finally:
        c.close()


# ── Migration 171 ──────────────────────────────────────────────────────────


class TestMigration171(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = _fresh_db()
        _RouteFixture.boot(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        _RouteFixture.teardown()

    def test_columns_present(self):
        c = _conn_test(self.db_path)
        try:
            cols = {r["name"] for r in c.execute(
                "PRAGMA table_info(user_onboarding)"
            )}
        finally:
            c.close()
        for col in ("tour_completed_at", "tour_skipped", "tour_skipped_at"):
            self.assertIn(col, cols, f"column {col} missing")

    def test_default_values(self):
        uid = _make_user(self.db_path)
        _ensure_onboarding_row(self.db_path, uid)
        row = _read_onboarding(self.db_path, uid)
        self.assertIsNone(row["tour_completed_at"])
        self.assertEqual(int(row["tour_skipped"] or 0), 0)
        self.assertIsNone(row["tour_skipped_at"])


# ── /api/onboarding/tour-state ─────────────────────────────────────────────


class TestTourStateGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = _fresh_db()
        _RouteFixture.boot(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        _RouteFixture.teardown()

    def test_no_onboarding_row_does_not_show(self):
        uid = _make_user(self.db_path)
        r = _RouteFixture.client.get(
            "/api/onboarding/tour-state", headers=_RouteFixture.as_user(uid),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["should_show"])

    def test_flow_incomplete_does_not_show(self):
        uid = _make_user(self.db_path)
        _ensure_onboarding_row(self.db_path, uid)
        r = _RouteFixture.client.get(
            "/api/onboarding/tour-state", headers=_RouteFixture.as_user(uid),
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["should_show"])

    def test_flow_complete_does_show(self):
        uid = _make_user(self.db_path)
        _mark_flow_complete(self.db_path, uid)
        r = _RouteFixture.client.get(
            "/api/onboarding/tour-state", headers=_RouteFixture.as_user(uid),
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["should_show"])


# ── /api/onboarding/tour-complete ──────────────────────────────────────────


class TestTourComplete(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = _fresh_db()
        _RouteFixture.boot(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        _RouteFixture.teardown()

    def test_complete_stamps_timestamp(self):
        uid = _make_user(self.db_path)
        _mark_flow_complete(self.db_path, uid)
        r = _RouteFixture.client.post(
            "/api/onboarding/tour-complete", json={},
            headers=_RouteFixture.as_user(uid),
        )
        self.assertEqual(r.status_code, 200, r.text)
        row = _read_onboarding(self.db_path, uid)
        self.assertIsNotNone(row["tour_completed_at"])

    def test_complete_is_idempotent(self):
        uid = _make_user(self.db_path)
        _mark_flow_complete(self.db_path, uid)
        _RouteFixture.client.post(
            "/api/onboarding/tour-complete", json={},
            headers=_RouteFixture.as_user(uid),
        )
        first = _read_onboarding(self.db_path, uid)["tour_completed_at"]
        time.sleep(1.1)
        _RouteFixture.client.post(
            "/api/onboarding/tour-complete", json={},
            headers=_RouteFixture.as_user(uid),
        )
        second = _read_onboarding(self.db_path, uid)["tour_completed_at"]
        self.assertEqual(first, second, "COALESCE should preserve first ts")

    def test_complete_flips_should_show_off(self):
        uid = _make_user(self.db_path)
        _mark_flow_complete(self.db_path, uid)
        before = _RouteFixture.client.get(
            "/api/onboarding/tour-state", headers=_RouteFixture.as_user(uid),
        ).json()
        self.assertTrue(before["should_show"])
        _RouteFixture.client.post(
            "/api/onboarding/tour-complete", json={},
            headers=_RouteFixture.as_user(uid),
        )
        after = _RouteFixture.client.get(
            "/api/onboarding/tour-state", headers=_RouteFixture.as_user(uid),
        ).json()
        self.assertFalse(after["should_show"])


# ── /api/onboarding/tour-skip ──────────────────────────────────────────────


class TestTourSkip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = _fresh_db()
        _RouteFixture.boot(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        _RouteFixture.teardown()

    def test_skip_sets_flag_and_timestamp(self):
        uid = _make_user(self.db_path)
        _mark_flow_complete(self.db_path, uid)
        r = _RouteFixture.client.post(
            "/api/onboarding/tour-skip", json={},
            headers=_RouteFixture.as_user(uid),
        )
        self.assertEqual(r.status_code, 200, r.text)
        row = _read_onboarding(self.db_path, uid)
        self.assertEqual(int(row["tour_skipped"]), 1)
        self.assertIsNotNone(row["tour_skipped_at"])

    def test_skip_flips_should_show_off(self):
        uid = _make_user(self.db_path)
        _mark_flow_complete(self.db_path, uid)
        self.assertTrue(_RouteFixture.client.get(
            "/api/onboarding/tour-state", headers=_RouteFixture.as_user(uid),
        ).json()["should_show"])
        _RouteFixture.client.post(
            "/api/onboarding/tour-skip", json={},
            headers=_RouteFixture.as_user(uid),
        )
        self.assertFalse(_RouteFixture.client.get(
            "/api/onboarding/tour-state", headers=_RouteFixture.as_user(uid),
        ).json()["should_show"])

    def test_skip_then_complete_does_not_unhide(self):
        uid = _make_user(self.db_path)
        _mark_flow_complete(self.db_path, uid)
        _RouteFixture.client.post(
            "/api/onboarding/tour-skip", json={},
            headers=_RouteFixture.as_user(uid),
        )
        _RouteFixture.client.post(
            "/api/onboarding/tour-complete", json={},
            headers=_RouteFixture.as_user(uid),
        )
        self.assertFalse(_RouteFixture.client.get(
            "/api/onboarding/tour-state", headers=_RouteFixture.as_user(uid),
        ).json()["should_show"])


# ── First-week goals widget ────────────────────────────────────────────────


class TestFirstWeekGoals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = _fresh_db()
        _RouteFixture.boot(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        _RouteFixture.teardown()

    def test_initial_state_visible_for_new_user(self):
        uid = _make_user(self.db_path)
        _ensure_onboarding_row(self.db_path, uid)
        r = _RouteFixture.client.get(
            "/api/first-week/goals", headers=_RouteFixture.as_user(uid),
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # 6 goals defined in onboarding_routes.ALL_GOALS
        self.assertEqual(body["total"], 6)
        self.assertEqual(body["completed_count"], 0)
        self.assertFalse(body["hide_widget"])

    def test_goals_alias_endpoint_works(self):
        uid = _make_user(self.db_path)
        _ensure_onboarding_row(self.db_path, uid)
        r = _RouteFixture.client.get(
            "/api/onboarding/goals", headers=_RouteFixture.as_user(uid),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 6)

    def test_completing_all_goals_hides_widget(self):
        uid = _make_user(self.db_path)
        _ensure_onboarding_row(self.db_path, uid)
        for key in (
            "follow_3_sources", "save_1_prediction", "enable_notifications",
            "visit_5_distinct_tabs", "view_1_market_detail",
            "complete_first_prediction",
        ):
            r = _RouteFixture.client.post(
                f"/api/first-week/goals/{key}", json={},
                headers=_RouteFixture.as_user(uid),
            )
            self.assertEqual(r.status_code, 200, r.text)
        body = _RouteFixture.client.get(
            "/api/first-week/goals", headers=_RouteFixture.as_user(uid),
        ).json()
        self.assertEqual(body["completed_count"], 6)
        self.assertTrue(body["hide_widget"])

    def test_dismiss_widget_hides_immediately(self):
        uid = _make_user(self.db_path)
        _ensure_onboarding_row(self.db_path, uid)
        r = _RouteFixture.client.post(
            "/api/first-week/widget/dismiss", json={},
            headers=_RouteFixture.as_user(uid),
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = _RouteFixture.client.get(
            "/api/first-week/goals", headers=_RouteFixture.as_user(uid),
        ).json()
        self.assertTrue(body["hide_widget"])
        self.assertTrue(body["dismissed"])


if __name__ == "__main__":
    unittest.main()
