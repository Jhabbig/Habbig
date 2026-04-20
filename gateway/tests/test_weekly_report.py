"""Tests for the weekly intelligence report pipeline."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_ACCESS_TOKEN", "test_token_48_chars_aaaaaaaaaaaaaaaaaaaaaaaaaaaa")

# Use isolated DB
_tmp_db = tempfile.NamedTemporaryFile(suffix="_report_test.db", delete=False)
_tmp_db.close()
os.environ["GATEWAY_DB_PATH"] = _tmp_db.name


class TestWeekBounds(unittest.TestCase):
    """get_week_bounds returns correct Monday-to-Sunday ranges."""

    def test_wednesday_returns_current_week(self):
        import datetime as dt
        from reports.weekly_report import get_week_bounds

        # Wednesday April 8, 2026 14:00 UTC
        ref = dt.datetime(2026, 4, 8, 14, 0, tzinfo=dt.timezone.utc)
        ws, we = get_week_bounds(ref)
        start = dt.datetime.fromtimestamp(ws, tz=dt.timezone.utc)
        end = dt.datetime.fromtimestamp(we, tz=dt.timezone.utc)
        self.assertEqual(start.weekday(), 0)  # Monday
        self.assertEqual((end - start).days, 7)

    def test_monday_morning_returns_previous_week(self):
        import datetime as dt
        from reports.weekly_report import get_week_bounds

        # Monday April 6, 2026 06:00 UTC (before noon)
        ref = dt.datetime(2026, 4, 6, 6, 0, tzinfo=dt.timezone.utc)
        ws, we = get_week_bounds(ref)
        start = dt.datetime.fromtimestamp(ws, tz=dt.timezone.utc)
        # Should be the previous Monday (March 30)
        self.assertEqual(start.weekday(), 0)
        self.assertEqual(start.day, 30)
        self.assertEqual(start.month, 3)

    def test_week_is_exactly_7_days(self):
        from reports.weekly_report import get_week_bounds
        ws, we = get_week_bounds()
        self.assertEqual(we - ws, 7 * 86400)


class TestReportDataCollection(unittest.TestCase):
    """collect_report_data returns the expected structure."""

    @classmethod
    def setUpClass(cls):
        import importlib
        import db
        importlib.reload(db)
        db.init_db()

        # Create a test user
        cls.user_id = db.create_user("report@test.com", "TestPass123!", username="reportuser")

    def test_returns_required_keys(self):
        from reports.weekly_report import collect_report_data, get_week_bounds
        ws, we = get_week_bounds()
        data = collect_report_data(self.user_id, ws, we)

        required_keys = [
            "resolved_predictions", "top_sources", "high_cred_predictions",
            "week_predictions", "total_predictions", "total_markets",
            "high_cred_correct", "high_cred_total", "user_id",
            "display_name", "user_topics", "followed_sources",
            "week_start", "week_end",
        ]
        for key in required_keys:
            self.assertIn(key, data, f"missing key: {key}")

    def test_empty_week_has_zero_predictions(self):
        from reports.weekly_report import collect_report_data, get_week_bounds
        ws, we = get_week_bounds()
        data = collect_report_data(self.user_id, ws, we)
        self.assertEqual(data["total_predictions"], 0)
        self.assertEqual(data["total_markets"], 0)
        self.assertEqual(data["high_cred_total"], 0)

    def test_display_name_populated(self):
        from reports.weekly_report import collect_report_data, get_week_bounds
        ws, we = get_week_bounds()
        data = collect_report_data(self.user_id, ws, we)
        self.assertEqual(data["display_name"], "reportuser")


class TestPlaceholderNarratives(unittest.TestCase):
    """When Claude is unavailable, placeholder narratives are returned."""

    def test_placeholder_has_executive_summary(self):
        from reports.weekly_report import _placeholder_narratives
        data = {
            "total_predictions": 42,
            "total_markets": 10,
            "high_cred_correct": 5,
            "high_cred_total": 7,
        }
        narratives = _placeholder_narratives(data)
        self.assertIn("executive_summary", narratives)
        self.assertIn("42", narratives["executive_summary"])
        self.assertIn("best_bets_analysis", narratives)
        self.assertIsInstance(narratives["best_bets_analysis"], list)


class TestReportHtmlRendering(unittest.TestCase):
    """render_report_html produces valid HTML."""

    def test_html_contains_required_sections(self):
        from reports.weekly_report import render_report_html
        import datetime as dt

        now = int(time.time())
        data = {
            "week_start": now - 7 * 86400,
            "week_end": now,
            "display_name": "TestUser",
            "total_predictions": 100,
            "total_markets": 25,
            "high_cred_correct": 8,
            "high_cred_total": 10,
            "resolved_predictions": [],
            "top_sources": [],
            "user_topics": [],
        }
        narratives = {
            "executive_summary": "Test executive summary.",
            "best_bets_analysis": [],
            "notable_source": "Test notable source.",
            "markets_to_watch": ["Market A will resolve Friday."],
        }
        html = render_report_html(data, narratives)

        self.assertIn("NARVE.AI INTELLIGENCE REPORT", html)
        self.assertIn("TestUser", html)
        self.assertIn("Test executive summary.", html)
        self.assertIn("Test notable source.", html)
        self.assertIn("Market A will resolve Friday.", html)
        self.assertIn("100", html)  # total predictions
        self.assertIn("25", html)   # total markets
        self.assertIn("80.0%", html)  # high-cred accuracy

    def test_html_escapes_user_data(self):
        from reports.weekly_report import render_report_html
        now = int(time.time())
        data = {
            "week_start": now - 7 * 86400,
            "week_end": now,
            "display_name": '<script>alert("xss")</script>',
            "total_predictions": 0, "total_markets": 0,
            "high_cred_correct": 0, "high_cred_total": 0,
            "resolved_predictions": [], "top_sources": [], "user_topics": [],
        }
        narratives = {"executive_summary": "", "best_bets_analysis": [], "notable_source": "", "markets_to_watch": []}
        html = render_report_html(data, narratives)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)


class TestPdfRendering(unittest.TestCase):
    """render_pdf produces bytes output (even without WeasyPrint)."""

    def test_render_returns_bytes(self):
        from reports.weekly_report import render_pdf
        result = render_pdf("<html><body><h1>Test</h1></body></html>")
        self.assertIsInstance(result, bytes)
        self.assertGreater(len(result), 0)


class TestDbModel(unittest.TestCase):
    """WeeklyReport DB operations."""

    @classmethod
    def setUpClass(cls):
        import importlib
        import db
        importlib.reload(db)
        db.init_db()
        # Run migrations so weekly_reports table gets created
        try:
            import migrations
            migrations.upgrade_to_head()
        except Exception as e:
            # If migrations fail (e.g. missing tables they depend on),
            # create the weekly_reports table directly as a fallback.
            with db.conn() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS weekly_reports (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        week_start INTEGER NOT NULL,
                        week_end INTEGER NOT NULL,
                        generated_at INTEGER NOT NULL,
                        delivered_at INTEGER,
                        pdf_path TEXT,
                        best_bets_correct INTEGER DEFAULT 0,
                        best_bets_total INTEGER DEFAULT 0,
                        simulated_roi_pct REAL DEFAULT 0.0,
                        top_signal_market TEXT,
                        top_source_handle TEXT,
                        total_predictions INTEGER DEFAULT 0,
                        total_markets INTEGER DEFAULT 0,
                        high_cred_accuracy REAL,
                        UNIQUE(user_id, week_start)
                    )
                """)
        cls.user_id = db.create_user("dbreport@test.com", "TestPass123!", username="dbreportuser")

    def test_upsert_creates_report(self):
        import db
        now = int(time.time())
        ws = now - 7 * 86400
        report_id = db.upsert_weekly_report(
            user_id=self.user_id,
            week_start=ws,
            week_end=now,
            pdf_path="test/path.pdf",
            best_bets_correct=3,
            best_bets_total=5,
            simulated_roi_pct=8.2,
            total_predictions=42,
            total_markets=10,
        )
        self.assertGreater(report_id, 0)

    def test_list_reports_returns_newest_first(self):
        import db
        now = int(time.time())
        # Create two reports for different weeks
        db.upsert_weekly_report(user_id=self.user_id, week_start=now - 14 * 86400, week_end=now - 7 * 86400, total_predictions=10)
        db.upsert_weekly_report(user_id=self.user_id, week_start=now - 7 * 86400, week_end=now, total_predictions=20)
        reports = db.list_weekly_reports(self.user_id)
        self.assertGreaterEqual(len(reports), 2)
        # Newest first
        self.assertGreaterEqual(reports[0]["week_start"], reports[1]["week_start"])

    def test_get_report_by_id(self):
        import db
        now = int(time.time())
        rid = db.upsert_weekly_report(user_id=self.user_id, week_start=now - 28 * 86400, week_end=now - 21 * 86400, total_predictions=5)
        report = db.get_weekly_report(rid)
        self.assertIsNotNone(report)
        self.assertEqual(report["user_id"], self.user_id)

    def test_mark_delivered(self):
        import db
        now = int(time.time())
        rid = db.upsert_weekly_report(user_id=self.user_id, week_start=now - 35 * 86400, week_end=now - 28 * 86400)
        db.mark_report_delivered(rid)
        report = db.get_weekly_report(rid)
        self.assertIsNotNone(report["delivered_at"])

    def test_upsert_is_idempotent(self):
        """Same user + week_start → updates the existing row, not a duplicate."""
        import db
        now = int(time.time())
        ws = now - 21 * 86400
        id1 = db.upsert_weekly_report(user_id=self.user_id, week_start=ws, week_end=ws + 7 * 86400, total_predictions=10)
        id2 = db.upsert_weekly_report(user_id=self.user_id, week_start=ws, week_end=ws + 7 * 86400, total_predictions=20)
        self.assertEqual(id1, id2)
        report = db.get_weekly_report(id1)
        self.assertEqual(report["total_predictions"], 20)  # updated, not duplicated

    def test_user_a_cannot_see_user_b_report(self):
        """IDOR prevention: list_weekly_reports is scoped to user_id."""
        import db
        other_user = db.create_user("other@test.com", "TestPass123!", username="otheruser")
        other_reports = db.list_weekly_reports(other_user)
        self.assertEqual(len(other_reports), 0)


class TestGetReportDataForWeek(unittest.TestCase):
    """db.get_report_data_for_week returns correct structure."""

    @classmethod
    def setUpClass(cls):
        import importlib
        import db
        importlib.reload(db)
        db.init_db()

    def test_returns_required_keys(self):
        import db
        now = int(time.time())
        data = db.get_report_data_for_week(now - 7 * 86400, now)
        for key in ["resolved_predictions", "top_sources", "high_cred_predictions",
                     "week_predictions", "total_predictions", "total_markets",
                     "high_cred_correct", "high_cred_total"]:
            self.assertIn(key, data, f"missing key: {key}")

    def test_empty_week_returns_zeros(self):
        import db
        now = int(time.time())
        data = db.get_report_data_for_week(now - 7 * 86400, now)
        self.assertEqual(data["total_predictions"], 0)
        self.assertIsInstance(data["resolved_predictions"], list)


class TestApiRoutes(unittest.TestCase):
    """API routes require Pro tier and return correct responses."""

    @classmethod
    def setUpClass(cls):
        import importlib
        import db
        importlib.reload(db)
        import server
        try:
            importlib.reload(server)
        except Exception:
            pass
        from fastapi.testclient import TestClient
        cls.client = TestClient(server.app)

    def test_reports_list_blocked_without_auth(self):
        """Unauthenticated request is redirected to gate, never gets report data."""
        r = self.client.get("/api/reports/weekly", follow_redirects=False)
        # Gate middleware redirects to /gate (302), or the endpoint returns 401/403
        self.assertIn(r.status_code, (302, 401, 403))

    def test_report_download_blocked_without_auth(self):
        r = self.client.get("/api/reports/weekly/1/pdf", follow_redirects=False)
        self.assertIn(r.status_code, (302, 401, 403, 404))

    def test_generate_blocked_without_auth(self):
        r = self.client.post("/api/reports/weekly/generate", follow_redirects=False)
        self.assertIn(r.status_code, (302, 401, 403))


if __name__ == "__main__":
    unittest.main()
