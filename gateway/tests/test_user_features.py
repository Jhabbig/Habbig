"""Tests for Features 11-14: global search, saved predictions, source following, odds chart.

Follows the same pattern as test_topics.py — one in-memory SQLite per test
class with db.conn monkey-patched onto it.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


def _make_test_db():
    """Build a fresh in-memory SQLite with the full schema + migrations applied."""
    test_conn = sqlite3.connect(":memory:")
    test_conn.row_factory = sqlite3.Row
    test_conn.execute("PRAGMA foreign_keys = ON")
    test_conn.executescript(db.SCHEMA)

    @contextlib.contextmanager
    def _fake_conn():
        try:
            yield test_conn
            test_conn.commit()
        except Exception:
            test_conn.rollback()
            raise

    original = db.conn
    db.conn = _fake_conn
    try:
        db.init_db()  # applies migrations incl. FTS5 virtual tables + triggers
    except Exception:
        db.conn = original
        raise
    return test_conn, original


def _restore_db(original_conn_fn):
    db.conn = original_conn_fn


class TestGlobalSearch(unittest.TestCase):
    """Feature 11: SQLite FTS5 global search."""

    @classmethod
    def setUpClass(cls):
        cls.test_conn, cls.original = _make_test_db()
        # Seed
        db.upsert_source_credibility("cryptoking", global_credibility=0.81, accuracy_unlocked=1)
        db.upsert_source_credibility("politicsguru", global_credibility=0.74, accuracy_unlocked=1)
        db.upsert_source_credibility("dogelord", global_credibility=0.42, accuracy_unlocked=0)
        cls.pid_btc = db.create_prediction(
            "cryptoking", "Bitcoin will exceed $100k by Q2 2026",
            category="crypto", market_id="btc-100k-q2", direction="YES",
            predicted_probability=0.74,
        )
        cls.pid_az = db.create_prediction(
            "politicsguru", "Democrats will win Arizona Senate race",
            category="politics", market_id="az-senate", direction="YES",
            predicted_probability=0.62,
        )
        cls.pid_doge = db.create_prediction(
            "dogelord", "DOGE to the moon", category="crypto",
            market_id="doge-moon", direction="YES", predicted_probability=0.9,
        )
        db.insert_market_snapshot(
            "btc-100k-q2", 0.55,
            snapshotted_at=int(time.time()) - 1000,
            market_question="Bitcoin over $100k by Q2 2026?",
            category="crypto",
        )

    @classmethod
    def tearDownClass(cls):
        _restore_db(cls.original)

    def test_predictions_search_returns_relevant_result(self):
        results = db.search_predictions("bitcoin")
        self.assertEqual(len(results), 1)
        self.assertIn("Bitcoin", results[0]["content"])
        # Credibility joined in
        self.assertEqual(results[0]["global_credibility"], 0.81)

    def test_highlight_wraps_match_in_mark_tags(self):
        results = db.search_predictions("bitcoin")
        self.assertIn("<mark>", results[0]["highlight"])
        self.assertIn("</mark>", results[0]["highlight"])

    def test_search_across_multiple_rows(self):
        results = db.search_predictions("win")
        # "Democrats will WIN" should match
        self.assertGreaterEqual(len(results), 1)
        handles = {r["source_handle"] for r in results}
        self.assertIn("politicsguru", handles)

    def test_empty_query_returns_empty_list_not_error(self):
        self.assertEqual(db.search_predictions(""), [])
        self.assertEqual(db.search_predictions("   "), [])
        self.assertEqual(db.search_sources(""), [])
        self.assertEqual(db.search_markets(""), [])

    def test_special_characters_do_not_crash(self):
        # FTS5 operators / quotes should be safely escaped, not raise.
        for q in ['"bitcoin"', "(bitcoin)", "bitcoin*", "bitcoin:crypto", "bitcoin AND OR"]:
            results = db.search_predictions(q)
            self.assertIsInstance(results, list)

    def test_sources_search(self):
        results = db.search_sources("crypto")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_handle"], "cryptoking")

    def test_markets_search_with_question_text(self):
        results = db.search_markets("bitcoin")
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["market_slug"], "btc-100k-q2")

    def test_markets_fts_survives_price_updates(self):
        """Repro of the backfill bug: a later snapshot with no market_question
        must not cause search_markets to lose the slug."""
        db.insert_market_snapshot("btc-100k-q2", 0.62, snapshotted_at=int(time.time()))
        results = db.search_markets("bitcoin")
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["market_slug"], "btc-100k-q2")
        # Latest price should now be 0.62
        self.assertEqual(results[0]["yes_price"], 0.62)

    def test_fts_sanitize_rejects_empty(self):
        self.assertEqual(db._fts_sanitize_query(""), "")
        self.assertEqual(db._fts_sanitize_query("   "), "")

    def test_fts_sanitize_escapes_quotes(self):
        out = db._fts_sanitize_query('bit"coin')
        self.assertIn('""', out)

    def test_prefix_wildcard_matches_partial(self):
        # "demo" should match "democrats"
        results = db.search_predictions("demo")
        self.assertGreaterEqual(len(results), 1)


class TestSavedPredictions(unittest.TestCase):
    """Feature 12: saved predictions / watchlist."""

    @classmethod
    def setUpClass(cls):
        cls.test_conn, cls.original = _make_test_db()
        cls.alice = db.create_user("alice@example.com", "AbcdefGhij1!", username="alice")
        cls.bob = db.create_user("bob@example.com", "AbcdefGhij1!", username="bob")
        db.upsert_source_credibility("src", global_credibility=0.7, accuracy_unlocked=1)
        cls.pid1 = db.create_prediction("src", "Test prediction one", market_id="m1")
        cls.pid2 = db.create_prediction("src", "Test prediction two", market_id="m2")

    @classmethod
    def tearDownClass(cls):
        _restore_db(cls.original)

    def test_save_creates_row(self):
        sid = db.save_prediction(self.alice, self.pid1, notes="Interesting")
        self.assertGreater(sid, 0)
        self.assertTrue(db.is_prediction_saved(self.alice, self.pid1))

    def test_cannot_save_nonexistent_prediction(self):
        sid = db.save_prediction(self.alice, 9999999)
        self.assertEqual(sid, 0)

    def test_duplicate_save_returns_same_id(self):
        sid1 = db.save_prediction(self.alice, self.pid2)
        sid2 = db.save_prediction(self.alice, self.pid2)
        self.assertEqual(sid1, sid2)

    def test_user_isolation(self):
        # Bob cannot see Alice's saved items
        db.save_prediction(self.alice, self.pid1)
        alice_list = db.list_saved_predictions(self.alice)
        bob_list = db.list_saved_predictions(self.bob)
        self.assertTrue(any(r["prediction_id"] == self.pid1 for r in alice_list))
        self.assertFalse(any(r["prediction_id"] == self.pid1 for r in bob_list))
        self.assertTrue(db.is_prediction_saved(self.alice, self.pid1))
        self.assertFalse(db.is_prediction_saved(self.bob, self.pid1))

    def test_notes_update(self):
        db.save_prediction(self.alice, self.pid1, notes="initial")
        self.assertTrue(db.update_saved_prediction_notes(self.alice, self.pid1, "updated note"))
        rows = db.list_saved_predictions(self.alice)
        target = next(r for r in rows if r["prediction_id"] == self.pid1)
        self.assertEqual(target["notes"], "updated note")

    def test_unsave_removes_row(self):
        db.save_prediction(self.alice, self.pid2)
        self.assertTrue(db.unsave_prediction(self.alice, self.pid2))
        # Second unsave is a no-op
        self.assertFalse(db.unsave_prediction(self.alice, self.pid2))
        self.assertFalse(db.is_prediction_saved(self.alice, self.pid2))

    def test_resolved_filter(self):
        db.save_prediction(self.alice, self.pid1)
        db.save_prediction(self.alice, self.pid2)
        # Mark pid1 resolved correct
        with db.conn() as c:
            c.execute("UPDATE predictions SET resolved = 1, resolved_correct = 1, resolved_at = ? WHERE id = ?",
                      (int(time.time()), self.pid1))
        correct = db.list_saved_predictions(self.alice, resolved_filter="correct")
        active = db.list_saved_predictions(self.alice, resolved_filter="active")
        self.assertTrue(all(r["resolved_correct"] == 1 for r in correct))
        self.assertTrue(all(r["resolved"] == 0 for r in active))


class TestSourceFollowing(unittest.TestCase):
    """Feature 13: source following."""

    @classmethod
    def setUpClass(cls):
        cls.test_conn, cls.original = _make_test_db()
        cls.alice = db.create_user("alice@example.com", "AbcdefGhij1!", username="alice")
        cls.bob = db.create_user("bob@example.com", "AbcdefGhij1!", username="bob")
        db.upsert_source_credibility("newsource", global_credibility=0.66, accuracy_unlocked=1)

    @classmethod
    def tearDownClass(cls):
        _restore_db(cls.original)

    def test_follow_creates_row(self):
        fid = db.follow_source(self.alice, "newsource", platform="twitter",
                               notify_on_prediction=True, notify_min_credibility=0.7)
        self.assertGreater(fid, 0)
        self.assertTrue(db.is_following_source(self.alice, "newsource"))

    def test_duplicate_follow_idempotent(self):
        fid1 = db.follow_source(self.alice, "newsource")
        fid2 = db.follow_source(self.alice, "newsource")
        self.assertEqual(fid1, fid2)

    def test_unfollow(self):
        db.follow_source(self.alice, "newsource")
        self.assertTrue(db.unfollow_source(self.alice, "newsource"))
        self.assertFalse(db.unfollow_source(self.alice, "newsource"))
        self.assertFalse(db.is_following_source(self.alice, "newsource"))

    def test_user_isolation(self):
        db.follow_source(self.alice, "newsource")
        alice = db.list_followed_sources(self.alice)
        bob = db.list_followed_sources(self.bob)
        self.assertTrue(any(r["source_handle"] == "newsource" for r in alice))
        self.assertFalse(any(r["source_handle"] == "newsource" for r in bob))

    def test_update_preferences(self):
        db.follow_source(self.alice, "newsource", notify_on_prediction=False, notify_min_credibility=0.5)
        self.assertTrue(db.update_follow_preferences(self.alice, "newsource", True, 0.9))
        rows = db.list_followed_sources(self.alice)
        target = next(r for r in rows if r["source_handle"] == "newsource")
        self.assertEqual(target["notify_on_prediction"], 1)
        self.assertAlmostEqual(target["notify_min_credibility"], 0.9)

    def test_followed_handles_set_helper(self):
        db.follow_source(self.alice, "newsource")
        handles = db.followed_source_handles(self.alice)
        self.assertIn("newsource", handles)
        self.assertEqual(db.followed_source_handles(self.bob), set())

    def test_credibility_joined_in_list(self):
        db.follow_source(self.alice, "newsource")
        rows = db.list_followed_sources(self.alice)
        target = next(r for r in rows if r["source_handle"] == "newsource")
        self.assertEqual(target["global_credibility"], 0.66)


class TestHistoricalOddsChart(unittest.TestCase):
    """Feature 14: historical odds chart data."""

    @classmethod
    def setUpClass(cls):
        cls.test_conn, cls.original = _make_test_db()
        db.upsert_source_credibility("analyst", global_credibility=0.8, accuracy_unlocked=1)
        now = int(time.time())
        # Seed a timeline: price rose from 0.50 → 0.55 → 0.60 → 0.65
        cls.ts1 = now - 4000
        cls.ts2 = now - 3000
        cls.ts3 = now - 2000
        cls.ts4 = now - 1000
        db.insert_market_snapshot("btc-100k-q2", 0.50, snapshotted_at=cls.ts1,
                                  market_question="Bitcoin > $100k by Q2?", category="crypto")
        db.insert_market_snapshot("btc-100k-q2", 0.55, snapshotted_at=cls.ts2)
        db.insert_market_snapshot("btc-100k-q2", 0.60, snapshotted_at=cls.ts3)
        db.insert_market_snapshot("btc-100k-q2", 0.65, snapshotted_at=cls.ts4)
        # A prediction made between ts2 and ts3
        cls.pid = db.create_prediction(
            "analyst", "BTC will hit 100k", category="crypto",
            market_id="btc-100k-q2", direction="YES", predicted_probability=0.75,
        )
        # Force its extracted_at to sit on the timeline
        with db.conn() as c:
            c.execute("UPDATE predictions SET extracted_at = ? WHERE id = ?",
                      (cls.ts2 + 500, cls.pid))

    @classmethod
    def tearDownClass(cls):
        _restore_db(cls.original)

    def test_history_ordered_ascending(self):
        hist = db.get_market_history("btc-100k-q2")
        self.assertEqual(len(hist), 4)
        for a, b in zip(hist, hist[1:]):
            self.assertLessEqual(a["snapshotted_at"], b["snapshotted_at"])
        self.assertEqual(hist[0]["yes_price"], 0.50)
        self.assertEqual(hist[-1]["yes_price"], 0.65)

    def test_snapshot_at_time_returns_closest_prior(self):
        # Ask for a time between ts2 and ts3 — should get ts2's snapshot
        snap = db.get_market_snapshot_at("btc-100k-q2", self.ts2 + 100)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["yes_price"], 0.55)

    def test_prediction_markers_include_market_price_at_time(self):
        markers = db.get_prediction_markers_for_market("btc-100k-q2")
        self.assertEqual(len(markers), 1)
        m = markers[0]
        self.assertEqual(m["source_handle"], "analyst")
        self.assertEqual(m["global_credibility"], 0.8)
        # Market price at prediction time should be 0.55 (ts2's snapshot)
        self.assertEqual(m["market_yes_price_at_time"], 0.55)

    def test_empty_markers_for_market_with_no_predictions(self):
        db.insert_market_snapshot("orphan-slug", 0.5, market_question="Empty?", category="other")
        markers = db.get_prediction_markers_for_market("orphan-slug")
        self.assertEqual(markers, [])

    def test_latest_snapshot(self):
        latest = db.get_latest_market_snapshot("btc-100k-q2")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["yes_price"], 0.65)
        # market_question propagated by backfill to every later row
        self.assertEqual(latest["market_question"], "Bitcoin > $100k by Q2?")

    def test_history_limit_bounds_response_size(self):
        hist = db.get_market_history("btc-100k-q2", limit=2)
        self.assertEqual(len(hist), 2)


if __name__ == "__main__":
    unittest.main()
