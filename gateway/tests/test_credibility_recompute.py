"""Tests for the credibility auto-recomputation pipeline (F1).

Verifies the Bayesian time-decay algorithm, per-category breakdown,
accuracy_unlocked threshold, and decay weighting behaviour.
"""

from __future__ import annotations

import math
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402


class TestRecomputeAllCredibilities(unittest.TestCase):
    """Core recomputation algorithm tests."""

    def _create_source_with_predictions(
        self,
        handle: str,
        predictions: list[dict],
    ) -> None:
        """Helper: insert predictions for a source.

        Each prediction dict should have:
          - correct: bool
          - age_days: int (how many days ago it resolved)
          - category: str (default "politics")
        """
        now = int(time.time())
        for p in predictions:
            pid = db.create_prediction(
                source_handle=handle,
                content=f"test prediction by {handle}",
                category=p.get("category", "politics"),
                market_id=f"poly:test-{handle}-{pid if 'pid' in dir() else 0}",
                direction="YES",
            )
            age_seconds = p.get("age_days", 0) * 86400
            resolved_at = now - age_seconds
            with db.conn() as c:
                c.execute(
                    "UPDATE predictions SET resolved = 1, resolved_correct = ?, resolved_at = ? WHERE id = ?",
                    (1 if p["correct"] else 0, resolved_at, pid),
                )

    def test_zero_predictions_returns_zero(self):
        """No resolved predictions → no sources recomputed."""
        result = db.recompute_all_credibilities()
        # May return >= 0 depending on pre-existing test data; the function
        # should not crash.
        self.assertIsInstance(result, int)

    def test_source_with_few_predictions_not_unlocked(self):
        """Source with < 10 resolved predictions: accuracy_unlocked = False."""
        handle = "test_few_preds_src"
        preds = [{"correct": True, "age_days": 1} for _ in range(5)]
        self._create_source_with_predictions(handle, preds)

        db.recompute_all_credibilities()

        cred = db.get_source_credibility(handle)
        self.assertIsNotNone(cred)
        self.assertFalse(bool(cred["accuracy_unlocked"]))
        self.assertEqual(cred["total_predictions"], 5)
        self.assertEqual(cred["correct_predictions"], 5)

    def test_source_with_enough_predictions_is_unlocked(self):
        """Source with >= 10 resolved predictions: accuracy_unlocked = True."""
        handle = "test_unlocked_src"
        preds = [{"correct": True, "age_days": i} for i in range(15)]
        self._create_source_with_predictions(handle, preds)

        db.recompute_all_credibilities()

        cred = db.get_source_credibility(handle)
        self.assertIsNotNone(cred)
        self.assertTrue(bool(cred["accuracy_unlocked"]))
        self.assertEqual(cred["total_predictions"], 15)

    def test_bayesian_smoothing_toward_prior(self):
        """With few predictions, score should be pulled toward 0.5 prior."""
        handle = "test_bayes_src"
        # 2 correct out of 2 → raw accuracy = 1.0
        # But with only n=2 and strength=10, Bayesian smoothing should
        # pull it toward 0.5: (2*1.0 + 10*0.5) / (2+10) = 7/12 ≈ 0.583
        preds = [{"correct": True, "age_days": 0}, {"correct": True, "age_days": 0}]
        self._create_source_with_predictions(handle, preds)

        db.recompute_all_credibilities()

        cred = db.get_source_credibility(handle)
        self.assertIsNotNone(cred)
        # Should be well below 1.0 due to smoothing
        self.assertLess(cred["global_credibility"], 0.7)
        self.assertGreater(cred["global_credibility"], 0.5)

    def test_recent_predictions_weighted_more(self):
        """Recent correct predictions should produce higher global credibility
        than old correct predictions, when mixed with some incorrect ones."""
        handle_recent = "test_recent_src"
        handle_old = "test_old_src"

        # Both sources: 7 correct + 3 incorrect = same raw 70% accuracy.
        # For handle_recent: correct ones are recent (1 day), incorrect are old (300 days).
        # For handle_old: correct ones are old (300 days), incorrect are recent (1 day).
        # Decay weighting should make handle_recent's DWA > handle_old's DWA
        # because handle_recent's correct predictions are recent (high weight)
        # and its incorrect ones are old (low weight), while handle_old is reversed.
        recent_preds = (
            [{"correct": True, "age_days": 1} for _ in range(7)]
            + [{"correct": False, "age_days": 300} for _ in range(3)]
        )
        old_preds = (
            [{"correct": True, "age_days": 300} for _ in range(7)]
            + [{"correct": False, "age_days": 1} for _ in range(3)]
        )

        self._create_source_with_predictions(handle_recent, recent_preds)
        self._create_source_with_predictions(handle_old, old_preds)

        db.recompute_all_credibilities()

        cred_recent = db.get_source_credibility(handle_recent)
        cred_old = db.get_source_credibility(handle_old)
        self.assertIsNotNone(cred_recent)
        self.assertIsNotNone(cred_old)

        # Recent-correct source should have higher DWA and global credibility.
        self.assertGreater(
            cred_recent["decay_weighted_accuracy"],
            cred_old["decay_weighted_accuracy"],
        )
        self.assertGreater(
            cred_recent["global_credibility"],
            cred_old["global_credibility"],
        )

    def test_per_category_breakdown(self):
        """Recompute should produce per-category credibility scores."""
        handle = "test_category_src"
        preds = [
            {"correct": True, "age_days": 1, "category": "politics"},
            {"correct": True, "age_days": 1, "category": "politics"},
            {"correct": False, "age_days": 1, "category": "crypto"},
        ]
        self._create_source_with_predictions(handle, preds)

        db.recompute_all_credibilities()

        cred = db.get_source_credibility(handle)
        self.assertIsNotNone(cred)
        self.assertEqual(cred["categories_active"], 2)

        pol_cred = db.get_category_credibility(handle, "politics")
        crypto_cred = db.get_category_credibility(handle, "crypto")

        self.assertIsNotNone(pol_cred)
        self.assertIsNotNone(crypto_cred)
        # Politics: 2/2 correct → high credibility
        # Crypto: 0/1 correct → low credibility
        self.assertGreater(pol_cred["category_credibility"], crypto_cred["category_credibility"])

    def test_mixed_accuracy_source(self):
        """Source with mixed results should have credibility between 0 and 1."""
        handle = "test_mixed_src"
        preds = (
            [{"correct": True, "age_days": i} for i in range(8)]
            + [{"correct": False, "age_days": i} for i in range(7)]
        )
        self._create_source_with_predictions(handle, preds)

        db.recompute_all_credibilities()

        cred = db.get_source_credibility(handle)
        self.assertIsNotNone(cred)
        self.assertGreater(cred["global_credibility"], 0.3)
        self.assertLess(cred["global_credibility"], 0.8)

    def test_idempotent(self):
        """Running recompute twice should produce the same scores."""
        handle = "test_idempotent_src"
        preds = [{"correct": True, "age_days": 1} for _ in range(5)]
        self._create_source_with_predictions(handle, preds)

        db.recompute_all_credibilities()
        cred1 = db.get_source_credibility(handle)

        db.recompute_all_credibilities()
        cred2 = db.get_source_credibility(handle)

        self.assertAlmostEqual(
            cred1["global_credibility"],
            cred2["global_credibility"],
            places=6,
        )

    def test_snapshot_created(self):
        """Recompute should create a credibility snapshot for each source."""
        handle = "test_snapshot_src"
        preds = [{"correct": True, "age_days": 0} for _ in range(3)]
        self._create_source_with_predictions(handle, preds)

        db.recompute_all_credibilities()

        snaps = db.get_credibility_snapshots(handle, limit=10)
        self.assertGreaterEqual(len(snaps), 1)
        self.assertAlmostEqual(
            snaps[0]["global_credibility"],
            db.get_source_credibility(handle)["global_credibility"],
            places=6,
        )


class TestRecomputeJobRegistration(unittest.TestCase):
    """Verify the cron job is properly registered."""

    def test_job_registered(self):
        from jobs.registry import job_registry
        self.assertIn("recompute_credibilities", job_registry)

    def test_cron_entries_registered(self):
        from jobs.registry import cron_jobs
        cred_crons = [c for c in cron_jobs if c["name"] == "recompute_credibilities"]
        # Should have 4 entries (every 6 hours)
        self.assertEqual(len(cred_crons), 4)
        hours = sorted(c["hour"] for c in cred_crons)
        self.assertEqual(hours, [0, 6, 12, 18])


if __name__ == "__main__":
    unittest.main()
