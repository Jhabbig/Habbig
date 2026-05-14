"""Tests for the polished /admin/audit-log surface — search, filter,
stats, suspicious-pattern flag, and streaming CSV export.

Mirrors the seeded-DB session pattern from test_admin_health_monitor.py:
seed a real admin user, mint a session token, and pass the cookie via
TestClient. queries/audit.py is exercised both directly and through the
HTTP layer so the route-level integration is covered without a second
shadow harness.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: F401 — shared in-memory DB
from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import server  # noqa: E402
from queries import audit as audit_queries  # noqa: E402
from security import audit as audit_module  # noqa: E402


def _create_admin_session() -> tuple[int, str, str]:
    pid = os.getpid()
    email = f"audit_admin_{pid}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.create_user(
            email, "Password1!verylong", username=f"audit_admin_{pid}",
        )
    db.set_user_role(user_id, 2)  # super-admin
    token = db.create_session(user_id)
    try:
        db.mark_session_two_fa_verified(token)
    except Exception:
        pass
    return user_id, email, token


def _create_regular_session() -> str:
    pid = os.getpid()
    email = f"audit_user_{pid}@test.local"
    existing = db.get_user_by_email(email)
    if existing:
        uid = existing["id"]
        db.set_user_role(uid, 0)
    else:
        uid = db.create_user(email, "Password1!verylong", username=f"audit_user_{pid}")
        db.set_user_role(uid, 0)
    return db.create_session(uid)


def _wipe_audit_log():
    with db.conn() as c:
        c.execute("DELETE FROM audit_log")


def _insert(
    *,
    timestamp: int,
    admin_user_id: int = 1,
    admin_email: str = "alice@narve.ai",
    action: str = "user.view",
    target_type: str = "user",
    target_id: str = "100",
    target_description: str = "victim@example.com",
    notes: str = "",
):
    """Direct insert that bypasses security/audit.log_action so a test can
    control the timestamp (necessary for the suspicious-pattern rule).
    """
    with db.conn() as c:
        c.execute(
            "INSERT INTO audit_log ("
            "timestamp, admin_user_id, admin_email, action, target_type, "
            "target_id, target_description, before_state, after_state, "
            "ip_address, user_agent, request_id, notes"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
            (
                int(timestamp), admin_user_id, admin_email, action,
                target_type, target_id, target_description,
                "127.0.0.1", "pytest/1.0", "req-test", notes,
            ),
        )


class AuditLogPageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_uid, cls.admin_email, admin_token = _create_admin_session()
        cls.admin_cookies = {server.COOKIE_NAME: admin_token}
        cls.user_cookies = {server.COOKIE_NAME: _create_regular_session()}
        cls.client = TestClient(server.app)

    def setUp(self):
        _wipe_audit_log()

    # ── Auth ────────────────────────────────────────────────────────

    def test_page_rejects_anonymous(self):
        r = self.client.get("/admin/audit-log", cookies={}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303, 403))

    def test_page_rejects_non_admin(self):
        r = self.client.get(
            "/admin/audit-log", cookies=self.user_cookies, follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    # ── Page render ────────────────────────────────────────────────

    def test_page_renders_for_admin(self):
        _insert(timestamp=int(time.time()) - 60, action="user.suspend")
        r = self.client.get("/admin/audit-log", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        # Hero + filter rail anchors must be present.
        self.assertIn("Audit log", r.text)
        self.assertIn("audit-filters", r.text)
        self.assertIn("audit-stats", r.text)
        self.assertIn("audit-chips", r.text)
        # The seeded row's action label should be on the page.
        self.assertIn("Suspended user account", r.text)

    def test_page_shows_csv_export_link_with_filter(self):
        r = self.client.get(
            "/admin/audit-log?action=user.suspend", cookies=self.admin_cookies,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("/admin/audit-log/export.csv", r.text)
        self.assertIn("action=user.suspend", r.text)

    def test_page_shows_quick_range_chips(self):
        r = self.client.get("/admin/audit-log", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Today", r.text)
        self.assertIn("Last 24h", r.text)
        self.assertIn("Last 7d", r.text)
        self.assertIn("Last 30d", r.text)

    # ── Filter behaviour ───────────────────────────────────────────

    def test_filter_by_action_type(self):
        now = int(time.time())
        _insert(timestamp=now - 60, action="user.suspend",
                target_description="target-suspend-row")
        _insert(timestamp=now - 30, action="user.promote_admin",
                target_description="target-promote-row")

        r = self.client.get(
            "/admin/audit-log?action=user.suspend", cookies=self.admin_cookies,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("target-suspend-row", r.text)
        self.assertNotIn("target-promote-row", r.text)

    def test_filter_by_admin_email_substring(self):
        now = int(time.time())
        _insert(timestamp=now - 60, admin_email="alice@narve.ai",
                target_description="alice-row")
        _insert(timestamp=now - 30, admin_email="bob@narve.ai",
                target_description="bob-row")

        r = self.client.get(
            "/admin/audit-log?admin_email=alice", cookies=self.admin_cookies,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("alice-row", r.text)
        self.assertNotIn("bob-row", r.text)

    def test_filter_by_target_user_id(self):
        now = int(time.time())
        _insert(timestamp=now - 60, target_id="500",
                target_description="row-500")
        _insert(timestamp=now - 30, target_id="600",
                target_description="row-600")

        r = self.client.get(
            "/admin/audit-log?target_user_id=500", cookies=self.admin_cookies,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("row-500", r.text)
        self.assertNotIn("row-600", r.text)

    def test_date_range_filter_excludes_older_rows(self):
        now = int(time.time())
        old_ts = now - 10 * 86400
        recent_ts = now - 3600
        _insert(timestamp=old_ts, target_description="old-row")
        _insert(timestamp=recent_ts, target_description="recent-row")

        # 7-day chip should include recent but exclude 10-day-old row.
        r = self.client.get(
            "/admin/audit-log?range=7d", cookies=self.admin_cookies,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("recent-row", r.text)
        self.assertNotIn("old-row", r.text)

    # ── Query helpers ──────────────────────────────────────────────

    def test_search_audit_log_returns_total_and_cursor(self):
        now = int(time.time())
        for i in range(75):
            _insert(timestamp=now - i, target_description=f"t-{i}")
        rows, cursor, total = db.search_audit_log({}, limit=50)
        self.assertEqual(len(rows), 50)
        self.assertEqual(total, 75)
        self.assertIsNotNone(cursor)
        # Walk the cursor → page 2 should yield the remaining 25.
        rows2, cursor2, total2 = db.search_audit_log({}, limit=50, before_id=cursor)
        self.assertEqual(len(rows2), 25)
        self.assertEqual(total2, 75)
        self.assertIsNone(cursor2)

    def test_get_audit_stats_top_actions_and_admins(self):
        now = int(time.time())
        for i in range(5):
            _insert(timestamp=now - i, action="user.suspend",
                    admin_email="alice@narve.ai")
        for i in range(2):
            _insert(timestamp=now - i, action="user.view",
                    admin_email="bob@narve.ai")
        stats = db.get_audit_stats({})
        self.assertEqual(stats["total"], 7)
        actions = dict(stats["top_actions"])
        self.assertEqual(actions["user.suspend"], 5)
        self.assertEqual(actions["user.view"], 2)
        admins = dict(stats["top_admins"])
        self.assertEqual(admins["alice@narve.ai"], 5)
        self.assertEqual(admins["bob@narve.ai"], 2)

    # ── CSV export ────────────────────────────────────────────────

    def test_csv_export_renders_valid_csv(self):
        now = int(time.time())
        _insert(timestamp=now, action="user.suspend",
                admin_email="alice@narve.ai",
                target_description="csv-target")
        r = self.client.get(
            "/admin/audit-log/export.csv", cookies=self.admin_cookies,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"].split(";")[0], "text/csv")
        self.assertIn("attachment", r.headers["content-disposition"])
        # First line must be the CSV header.
        first_line = r.text.splitlines()[0]
        self.assertIn("timestamp_iso", first_line)
        self.assertIn("admin_email", first_line)
        self.assertIn("action", first_line)
        # The seeded row's fields should appear.
        self.assertIn("alice@narve.ai", r.text)
        self.assertIn("user.suspend", r.text)
        self.assertIn("csv-target", r.text)

    def test_csv_export_honours_filter(self):
        now = int(time.time())
        _insert(timestamp=now, action="user.suspend",
                target_description="will-be-in-csv")
        _insert(timestamp=now, action="user.view",
                target_description="filtered-out")
        r = self.client.get(
            "/admin/audit-log/export.csv?action=user.suspend",
            cookies=self.admin_cookies,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("will-be-in-csv", r.text)
        self.assertNotIn("filtered-out", r.text)

    def test_csv_export_rejects_non_admin(self):
        r = self.client.get(
            "/admin/audit-log/export.csv",
            cookies=self.user_cookies, follow_redirects=False,
        )
        self.assertEqual(r.status_code, 403)

    # ── Suspicious patterns ───────────────────────────────────────

    def test_suspicious_flag_fires_for_watermark_storm(self):
        """Six trace-watermark events from one admin inside one hour →
        the email.watermark_trace rule (>5 in 1h) must fire.
        """
        now = int(time.time())
        for i in range(6):
            _insert(
                timestamp=now - (60 * i),  # all within the same hour
                action=audit_module.AuditAction.EMAIL_WATERMARK_TRACE,
                admin_email="alice@narve.ai",
                target_id=str(1000 + i),
                target_description=f"trace-{i}",
            )
        flags = db.detect_suspicious_patterns({})
        watermark_flags = [
            f for f in flags
            if f["action"] == audit_module.AuditAction.EMAIL_WATERMARK_TRACE
        ]
        self.assertGreaterEqual(len(watermark_flags), 1)
        flag = watermark_flags[0]
        self.assertEqual(flag["admin_email"], "alice@narve.ai")
        self.assertGreaterEqual(flag["count"], 6)
        self.assertEqual(flag["threshold"], 5)

    def test_suspicious_flag_does_not_fire_below_threshold(self):
        """Five trace-watermark events in one hour is exactly the rule
        threshold (>= 5) and should fire; four is below threshold and
        must not surface.
        """
        now = int(time.time())
        for i in range(4):  # 4 events — below the >5 threshold
            _insert(
                timestamp=now - (60 * i),
                action=audit_module.AuditAction.EMAIL_WATERMARK_TRACE,
                admin_email="bob@narve.ai",
                target_id=str(2000 + i),
            )
        flags = db.detect_suspicious_patterns({})
        watermark_flags_for_bob = [
            f for f in flags
            if f["action"] == audit_module.AuditAction.EMAIL_WATERMARK_TRACE
            and f["admin_email"] == "bob@narve.ai"
        ]
        self.assertEqual(len(watermark_flags_for_bob), 0)

    def test_suspicious_flag_renders_on_page(self):
        now = int(time.time())
        for i in range(6):
            _insert(
                timestamp=now - (60 * i),
                action=audit_module.AuditAction.EMAIL_WATERMARK_TRACE,
                admin_email="alice@narve.ai",
                target_id=str(3000 + i),
            )
        r = self.client.get("/admin/audit-log", cookies=self.admin_cookies)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Suspicious patterns", r.text)
        self.assertIn("Watermark traces per admin in 1h", r.text)
        self.assertIn("alice@narve.ai", r.text)


class AuditLogFilterParsingTestCase(unittest.TestCase):
    """Quick coverage on the v2 filter-to-search-kwargs translator so a
    URL-level deep link doesn't silently lose a clause."""

    def test_range_chip_24h(self):
        class QP:
            def __init__(self, d): self._d = d
            def get(self, k, default=None): return self._d.get(k, default)
            def items(self): return self._d.items()

        before = int(time.time())
        kwargs = audit_module.filter_to_search_kwargs(QP({"range": "24h"}))
        self.assertIn("from_ts", kwargs)
        self.assertIn("to_ts", kwargs)
        self.assertGreaterEqual(kwargs["to_ts"], before - 1)
        # 24h window should be roughly one day wide.
        self.assertAlmostEqual(
            kwargs["to_ts"] - kwargs["from_ts"], 86400, delta=120,
        )

    def test_explicit_from_to(self):
        class QP:
            def __init__(self, d): self._d = d
            def get(self, k, default=None): return self._d.get(k, default)
            def items(self): return self._d.items()

        kwargs = audit_module.filter_to_search_kwargs(
            QP({"from": "2026-01-01", "to": "2026-12-31"}),
        )
        self.assertIn("from_ts", kwargs)
        self.assertIn("to_ts", kwargs)
        self.assertGreater(kwargs["to_ts"], kwargs["from_ts"])

    def test_admin_email_and_target_user(self):
        class QP:
            def __init__(self, d): self._d = d
            def get(self, k, default=None): return self._d.get(k, default)
            def items(self): return self._d.items()

        kwargs = audit_module.filter_to_search_kwargs(
            QP({"admin_email": "ALICE", "target_user_id": "42"}),
        )
        # filter_to_search_kwargs preserves casing — `queries/audit._normalise_filters`
        # lower-cases admin_email at SQL time so the substring match is case-insensitive.
        self.assertEqual(kwargs.get("admin_email"), "ALICE")
        self.assertEqual(kwargs.get("target_user_id"), "42")


if __name__ == "__main__":
    unittest.main()
