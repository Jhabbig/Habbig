"""Tests for the analytics event DB layer and IP hashing."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


class TestAnalyticsDb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._conn = sqlite3.connect(":memory:")
        cls._conn.row_factory = sqlite3.Row
        cls._conn.execute("PRAGMA foreign_keys = ON")

        @contextlib.contextmanager
        def fake_conn():
            try:
                yield cls._conn
                cls._conn.commit()
            except Exception:
                cls._conn.rollback()
                raise

        cls._orig = db.conn
        db.conn = fake_conn
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig
        cls._conn.close()

    def test_record_event(self):
        eid = db.record_analytics_event(
            event_type="page_view",
            user_id=None,
            session_id=None,
            page="/landing",
            referrer="",
            ip_hash="abc123",
            user_agent_category="desktop",
        )
        self.assertGreater(eid, 0)

    def test_ip_hash_never_raw_ip(self):
        # Spot check: a SHA-256-derived hex is way longer than a dotted IP.
        from server import _hash_ip
        h = _hash_ip("192.168.1.1")
        self.assertNotIn(".", h)
        self.assertNotIn("192", h)
        self.assertGreaterEqual(len(h), 16)

    def test_newsletter_signup_event_counted(self):
        for _ in range(3):
            db.record_analytics_event(
                "newsletter_signup", None, None, "/", "", "ip" + str(_), "desktop"
            )
        result = db.get_analytics_prerelease(since=0)
        self.assertGreaterEqual(result["newsletter_signups"], 3)

    def test_get_analytics_users_growth_series(self):
        db.create_user("growth1@test.com", "TestPass123!", username="growth1")
        db.create_user("growth2@test.com", "TestPass123!", username="growth2")
        result = db.get_analytics_users(since=0)
        self.assertGreaterEqual(result["total_users"], 2)
        self.assertIn("growth_series", result)

    def test_get_analytics_revenue_returns_breakdown(self):
        result = db.get_analytics_revenue()
        self.assertIn("mrr", result)
        self.assertIn("arr", result)
        self.assertIn("breakdown", result)
        self.assertEqual(result["arr"], result["mrr"] * 12)

    def test_get_analytics_features(self):
        db.record_analytics_event("feed_view", None, None, "/feed", "", "iphash1", "desktop")
        result = db.get_analytics_features(since=0)
        self.assertGreaterEqual(result["feed_views"], 1)


if __name__ == "__main__":
    unittest.main()
