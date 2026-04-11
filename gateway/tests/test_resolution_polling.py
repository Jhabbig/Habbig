"""Tests for market resolution auto-detection (F2).

Verifies the db helper functions for resolving predictions and the
job registration. API polling is tested via mocks in integration tests
(not here — these are unit tests for the data layer).
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402


class TestGetUnresolvedMarketIds(unittest.TestCase):
    def test_returns_empty_when_no_predictions(self):
        ids = db.get_unresolved_market_ids()
        self.assertIsInstance(ids, list)

    def test_returns_unresolved_market_ids(self):
        # Create predictions for 2 different markets
        db.create_prediction("src1", "pred A", market_id="poly:test-market-a", direction="YES")
        db.create_prediction("src2", "pred B", market_id="kalshi:test-market-b", direction="NO")
        ids = db.get_unresolved_market_ids()
        self.assertIn("poly:test-market-a", ids)
        self.assertIn("kalshi:test-market-b", ids)

    def test_excludes_resolved_predictions(self):
        pid = db.create_prediction("src3", "pred C", market_id="poly:resolved-mkt", direction="YES")
        with db.conn() as c:
            c.execute(
                "UPDATE predictions SET resolved = 1, resolved_correct = 1, resolved_at = ? WHERE id = ?",
                (int(time.time()), pid),
            )
        ids = db.get_unresolved_market_ids()
        self.assertNotIn("poly:resolved-mkt", ids)

    def test_excludes_null_market_id(self):
        db.create_prediction("src4", "pred D no market", market_id=None, direction="YES")
        ids = db.get_unresolved_market_ids()
        self.assertNotIn(None, ids)
        self.assertNotIn("", ids)


class TestResolvePredictionsForMarket(unittest.TestCase):
    def test_resolve_yes_outcome(self):
        """YES outcome: direction=YES → correct, direction=NO → incorrect."""
        market_id = "poly:yes-outcome-test"
        pid_yes = db.create_prediction("src_y", "yes pred", market_id=market_id, direction="YES")
        pid_no = db.create_prediction("src_n", "no pred", market_id=market_id, direction="NO")

        count = db.resolve_predictions_for_market(market_id, outcome_yes=True)
        self.assertEqual(count, 2)

        with db.conn() as c:
            row_yes = c.execute("SELECT * FROM predictions WHERE id = ?", (pid_yes,)).fetchone()
            row_no = c.execute("SELECT * FROM predictions WHERE id = ?", (pid_no,)).fetchone()

        self.assertEqual(row_yes["resolved"], 1)
        self.assertEqual(row_yes["resolved_correct"], 1)
        self.assertEqual(row_no["resolved"], 1)
        self.assertEqual(row_no["resolved_correct"], 0)

    def test_resolve_no_outcome(self):
        """NO outcome: direction=YES → incorrect, direction=NO → correct."""
        market_id = "poly:no-outcome-test"
        pid_yes = db.create_prediction("src_y2", "yes pred 2", market_id=market_id, direction="YES")
        pid_no = db.create_prediction("src_n2", "no pred 2", market_id=market_id, direction="NO")

        count = db.resolve_predictions_for_market(market_id, outcome_yes=False)
        self.assertEqual(count, 2)

        with db.conn() as c:
            row_yes = c.execute("SELECT * FROM predictions WHERE id = ?", (pid_yes,)).fetchone()
            row_no = c.execute("SELECT * FROM predictions WHERE id = ?", (pid_no,)).fetchone()

        self.assertEqual(row_yes["resolved_correct"], 0)
        self.assertEqual(row_no["resolved_correct"], 1)

    def test_idempotent(self):
        """Already-resolved predictions should not be re-updated."""
        market_id = "poly:idempotent-test"
        pid = db.create_prediction("src_idem", "idem pred", market_id=market_id, direction="YES")

        # Resolve first time
        count1 = db.resolve_predictions_for_market(market_id, outcome_yes=True)
        self.assertEqual(count1, 1)

        # Resolve again — should affect 0 rows
        count2 = db.resolve_predictions_for_market(market_id, outcome_yes=True)
        self.assertEqual(count2, 0)

    def test_null_direction_gets_null_correct(self):
        """Predictions without a direction get resolved_correct = NULL."""
        market_id = "poly:null-dir-test"
        pid = db.create_prediction("src_null", "ambiguous pred", market_id=market_id, direction=None)

        db.resolve_predictions_for_market(market_id, outcome_yes=True)

        with db.conn() as c:
            row = c.execute("SELECT * FROM predictions WHERE id = ?", (pid,)).fetchone()

        self.assertEqual(row["resolved"], 1)
        self.assertIsNone(row["resolved_correct"])

    def test_sets_resolved_at_timestamp(self):
        market_id = "poly:timestamp-test"
        db.create_prediction("src_ts", "ts pred", market_id=market_id, direction="YES")

        before = int(time.time())
        db.resolve_predictions_for_market(market_id, outcome_yes=True)
        after = int(time.time())

        with db.conn() as c:
            row = c.execute(
                "SELECT resolved_at FROM predictions WHERE market_id = ?", (market_id,)
            ).fetchone()

        self.assertGreaterEqual(row["resolved_at"], before)
        self.assertLessEqual(row["resolved_at"], after)


class TestResolutionJobRegistration(unittest.TestCase):
    def test_job_registered(self):
        from jobs.registry import job_registry
        self.assertIn("poll_market_resolutions", job_registry)

    def test_cron_entry_registered(self):
        from jobs.registry import cron_jobs
        res_crons = [c for c in cron_jobs if c["name"] == "poll_market_resolutions"]
        self.assertGreaterEqual(len(res_crons), 1)
        # Should run hourly at :17
        self.assertEqual(res_crons[0]["minute"], 17)
        self.assertIsNone(res_crons[0]["hour"])  # every hour


if __name__ == "__main__":
    unittest.main()
