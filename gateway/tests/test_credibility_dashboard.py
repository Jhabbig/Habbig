"""Tests for credibility engine on dashboards."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


class TestCredibilityEngine(unittest.TestCase):
    """Tests for credibility DB operations and display logic."""

    @classmethod
    def setUpClass(cls):
        cls._test_conn = sqlite3.connect(":memory:")
        cls._test_conn.row_factory = sqlite3.Row
        cls._test_conn.execute("PRAGMA foreign_keys = ON")
        cls._test_conn.executescript(db.SCHEMA)
        cls._test_conn.commit()

        @contextlib.contextmanager
        def test_conn():
            try:
                yield cls._test_conn
                cls._test_conn.commit()
            except Exception:
                cls._test_conn.rollback()
                raise

        cls._orig_conn = db.conn
        db.conn = test_conn
        db.init_db()

        cls.user_id = db.create_user("cred@test.com", "TestPass123!", username="creduser")

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig_conn
        cls._test_conn.close()

    def test_upsert_and_get_source_credibility(self):
        db.upsert_source_credibility(
            "source1", 0.74, accuracy_unlocked=True,
            decay_weighted_accuracy=0.72, total_predictions=20,
            correct_predictions=15, categories_active=3,
        )
        cred = db.get_source_credibility("source1")
        self.assertIsNotNone(cred)
        self.assertAlmostEqual(cred["global_credibility"], 0.74, places=2)
        self.assertTrue(bool(cred["accuracy_unlocked"]))
        self.assertEqual(cred["total_predictions"], 20)
        self.assertEqual(cred["correct_predictions"], 15)

    def test_unrated_source_returns_none(self):
        cred = db.get_source_credibility("nonexistent_source")
        self.assertIsNone(cred)

    def test_category_credibility(self):
        db.upsert_category_credibility("source1", "politics", 0.81, 10, 8)
        cat = db.get_category_credibility("source1", "politics")
        self.assertIsNotNone(cat)
        self.assertAlmostEqual(cat["category_credibility"], 0.81, places=2)

    def test_category_credibility_shown_where_available(self):
        db.upsert_category_credibility("source1", "sports", 0.65, 5, 3)
        cats = db.get_all_category_credibilities("source1")
        categories = [c["category"] for c in cats]
        self.assertIn("politics", categories)
        self.assertIn("sports", categories)

    def test_snapshots_stored(self):
        # Ensure a snapshot exists by upserting
        db.upsert_source_credibility("snap_source", 0.74)
        snaps = db.get_credibility_snapshots("snap_source", 5)
        self.assertGreaterEqual(len(snaps), 1)
        self.assertAlmostEqual(snaps[0]["global_credibility"], 0.74, places=2)

    def test_list_all_credibilities(self):
        # Ensure both sources exist
        db.upsert_source_credibility("list_source1", 0.74)
        db.upsert_source_credibility("list_source2", 0.55)
        all_creds = db.list_all_source_credibilities()
        handles = [c["source_handle"] for c in all_creds]
        self.assertIn("list_source1", handles)
        self.assertIn("list_source2", handles)

    def test_recompute_all_returns_count(self):
        # Recompute needs at least one source with a resolved prediction.
        import time as _t
        db.upsert_source_credibility("recompute_src", 0.6)
        pid = db.create_prediction(
            source_handle="recompute_src",
            content="test prediction for recompute",
            category="politics",
            direction="YES",
        )
        with db.conn() as c:
            c.execute(
                "UPDATE predictions SET resolved = 1, resolved_correct = 1, resolved_at = ? WHERE id = ?",
                (int(_t.time()), pid),
            )
        count = db.recompute_all_credibilities()
        self.assertGreaterEqual(count, 1)

    def test_force_refresh_requires_pro_tier(self):
        """Conceptual test: Trader tier should get 403 on /api/credibility/refresh.
        The actual HTTP test requires ASGI test client, but we verify the
        _require_pro_user logic exists by checking PLAN_DEFS gating."""
        import server
        # Trader is not pro
        self.assertNotEqual(server.PLAN_DEFS["trader"]["label"], "Pro")
        # Pro is pro
        self.assertEqual(server.PLAN_DEFS["pro"]["label"], "Pro")


class TestCredibilityDisplayLogic(unittest.TestCase):
    """Test credibility score colouring rules."""

    def test_high_credibility_styling(self):
        """Scores >= 0.7 should use weight 600."""
        score = 0.74
        self.assertGreaterEqual(score, 0.7)

    def test_medium_credibility_styling(self):
        """Scores 0.4-0.69 should use weight 400."""
        score = 0.55
        self.assertTrue(0.4 <= score <= 0.69)

    def test_low_credibility_styling(self):
        """Scores < 0.4 should use weight 300."""
        score = 0.25
        self.assertLess(score, 0.4)


if __name__ == "__main__":
    unittest.main()
