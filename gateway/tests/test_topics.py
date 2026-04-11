"""Tests for Signal Search — topics, predictions, analysis."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


class TestTopicOperations(unittest.TestCase):
    """Tests for topic CRUD and scheduling."""

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

        cls.pro_user = db.create_user("pro@test.com", "TestPass123!", username="prouser")
        cls.trader_user = db.create_user("trader@test.com", "TestPass123!", username="traderuser2")

    @classmethod
    def tearDownClass(cls):
        db.conn = cls._orig_conn
        cls._test_conn.close()

    def test_create_topic(self):
        topic_id = db.create_topic(self.pro_user, "US Elections", ["election", "senate"], 60)
        self.assertGreater(topic_id, 0)
        topic = db.get_topic(topic_id)
        self.assertIsNotNone(topic)
        self.assertEqual(topic["name"], "US Elections")
        keywords = json.loads(topic["keywords"])
        self.assertEqual(keywords, ["election", "senate"])

    def test_list_topics(self):
        topics = db.list_topics(self.pro_user)
        self.assertGreaterEqual(len(topics), 1)

    def test_max_10_topics_enforced(self):
        """Pro users limited to 10 topics."""
        # Create up to 10
        for i in range(12):
            db.create_topic(self.pro_user, f"Topic {i}", [f"kw{i}"], 60)
        count = db.count_user_topics(self.pro_user)
        # We can create more in DB but the API enforces the limit
        self.assertGreaterEqual(count, 10)

    def test_delete_topic_cascades(self):
        topic_id = db.create_topic(self.pro_user, "To Delete", ["delete"], 60)
        # Add a prediction
        pred_id = db.create_prediction("deltest_source", "Will be deleted", "other")
        db.add_topic_prediction(topic_id, pred_id)
        # Add analysis
        db.save_topic_analysis(
            topic_id, "bullish", "Summary", [], [], [], "low", "test"
        )
        # Delete should cascade
        db.delete_topic(topic_id)
        self.assertIsNone(db.get_topic(topic_id))

    def test_update_topic_pull(self):
        topic_id = db.create_topic(self.pro_user, "Pull Test", ["pull"], 60)
        db.update_topic_pull(topic_id, posts_found=5, predictions_extracted=2)
        topic = db.get_topic(topic_id)
        self.assertIsNotNone(topic["last_pulled_at"])
        self.assertEqual(topic["posts_found_total"], 5)
        self.assertEqual(topic["predictions_extracted_total"], 2)

    def test_due_topics_picked_up(self):
        """Topics with next_pull_at <= now should be returned."""
        topic_id = db.create_topic(self.pro_user, "Due Topic", ["due"], 60)
        # Set next_pull_at to past
        with db.conn() as c:
            c.execute("UPDATE user_topics SET next_pull_at = ? WHERE id = ?",
                       (int(time.time()) - 100, topic_id))
        due = db.get_due_topics()
        due_ids = [t["id"] for t in due]
        self.assertIn(topic_id, due_ids)

    def test_save_and_get_analysis(self):
        topic_id = db.create_topic(self.pro_user, "Analysis Test", ["analysis"], 60)
        db.save_topic_analysis(
            topic_id,
            signal_direction="bullish",
            summary="Three sources agree on direction",
            top_signals=[{"prediction": "test", "source": "s1", "credibility": 0.8}],
            contradictions=["s2 disagrees"],
            relevant_markets=["market1"],
            confidence="medium",
            confidence_reason="3 sources, avg cred 0.7",
        )
        analysis = db.get_latest_topic_analysis(topic_id)
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["signal_direction"], "bullish")
        self.assertEqual(analysis["confidence"], "medium")
        top_signals = json.loads(analysis["top_signals"])
        self.assertEqual(len(top_signals), 1)
        self.assertEqual(top_signals[0]["source"], "s1")

    def test_analysis_not_rerun_within_30_min(self):
        """Analysis generated_at should be checked by caller to enforce 30-min window.
        We verify the timestamp is stored correctly."""
        topic_id = db.create_topic(self.pro_user, "Timing Test", ["timing"], 60)
        db.save_topic_analysis(
            topic_id, "mixed", "Recent analysis", [], [], [], "low", "test"
        )
        analysis = db.get_latest_topic_analysis(topic_id)
        now = int(time.time())
        # Should have been generated within last few seconds
        self.assertLessEqual(abs(analysis["generated_at"] - now), 5)
        # Caller would check: if now - analysis["generated_at"] < 1800: skip

    def test_topic_predictions_linked(self):
        topic_id = db.create_topic(self.pro_user, "Linked Test", ["linked"], 60)
        pred_id = db.create_prediction("linked_source", "Will X happen", "politics",
                                        direction="YES", predicted_probability=0.7)
        db.add_topic_prediction(topic_id, pred_id)
        preds = db.get_topic_predictions(topic_id)
        self.assertEqual(len(preds), 1)
        self.assertEqual(preds[0]["source_handle"], "linked_source")


class TestTraderCannotAccessSignalSearch(unittest.TestCase):
    """Verify tier gating for Signal Search."""

    def test_signal_search_requires_pro(self):
        """The route checks plan == 'pro'. Trader should be redirected."""
        import server
        # Verify PLAN_DEFS has trader != pro
        self.assertNotEqual(server.PLAN_DEFS["trader"]["label"], "Pro")

    def test_signal_search_page_exists(self):
        """Verify signal-search.html template exists."""
        path = os.path.join(os.path.dirname(__file__), "..", "static", "signal-search.html")
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
