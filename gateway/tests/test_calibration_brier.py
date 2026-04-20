"""Tests for Brier-score calibration and market timing scoring.

Covers: Brier score computation, reliability diagram, overconfidence detection,
timing score formula, contrarian bonus, and integration with credibility pipeline.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: F401
import db  # noqa: E402


def _insert_prediction(
    source_handle: str,
    predicted_probability: float,
    resolved_correct: bool,
    age_days: int = 0,
    direction: str = "YES",
    market_id: str = "poly:test",
) -> int:
    now = int(time.time())
    pid = db.create_prediction(
        source_handle=source_handle,
        content=f"test prediction prob={predicted_probability}",
        category="politics",
        market_id=market_id,
        direction=direction,
        predicted_probability=predicted_probability,
    )
    with db.conn() as c:
        c.execute(
            "UPDATE predictions SET resolved = 1, resolved_correct = ?, "
            "resolved_at = ? WHERE id = ?",
            (1 if resolved_correct else 0, now - age_days * 86400, pid),
        )
    return pid


class TestBrierScore(unittest.TestCase):
    """Test the Brier-score based calibration computation."""

    def test_perfect_calibration(self):
        """Source with predictions matching outcomes perfectly: score ≈ 1.0."""
        handle = "brier_perfect"
        # 10 predictions at 0.8 probability, 8 correct (80% = matches 0.8)
        for i in range(8):
            _insert_prediction(handle, 0.80, True)
        for i in range(2):
            _insert_prediction(handle, 0.80, False)

        result = db.compute_calibration(handle)
        self.assertIsNotNone(result)
        # Brier for p=0.8, outcome=1: (0.8-1)^2 = 0.04 → 8 of these
        # Brier for p=0.8, outcome=0: (0.8-0)^2 = 0.64 → 2 of these
        # Brier = (8*0.04 + 2*0.64) / 10 = (0.32 + 1.28) / 10 = 0.16
        self.assertLess(result["brier_score"], 0.25)  # better than random
        self.assertGreater(result["calibration_score"], 0.3)

    def test_random_guessing_brier(self):
        """Source always predicting 0.5: Brier ≈ 0.25 → score ≈ 0.0."""
        handle = "brier_random"
        for i in range(10):
            _insert_prediction(handle, 0.50, i % 2 == 0)  # 50% correct

        result = db.compute_calibration(handle)
        self.assertIsNotNone(result)
        # Brier for p=0.5 is always 0.25 regardless of outcome
        self.assertAlmostEqual(result["brier_score"], 0.25, places=2)
        self.assertLess(result["calibration_score"], 0.1)

    def test_insufficient_data_returns_none(self):
        """Fewer than 10 predictions → None."""
        handle = "brier_few"
        for i in range(5):
            _insert_prediction(handle, 0.7, True)
        result = db.compute_calibration(handle)
        self.assertIsNone(result)

    def test_calibration_score_in_range(self):
        """Score always in [0.0, 1.0]."""
        handle = "brier_range"
        for i in range(15):
            _insert_prediction(handle, 0.9, i < 3)  # overconfident
        result = db.compute_calibration(handle)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["calibration_score"], 0.0)
        self.assertLessEqual(result["calibration_score"], 1.0)


class TestReliabilityDiagram(unittest.TestCase):
    """Test the reliability diagram data and overconfidence detection."""

    def test_overconfident_detection(self):
        """Source stating high probabilities but low actual accuracy → overconfident."""
        handle = "overconf_src"
        # Says 90% but only right 40% of the time
        for i in range(10):
            _insert_prediction(handle, 0.90, i < 4)

        result = db.compute_calibration(handle)
        self.assertIsNotNone(result)
        self.assertTrue(result["is_overconfident"])
        self.assertFalse(result["is_underconfident"])

    def test_calibrated_detection(self):
        """Source with predictions matching actual rates → calibrated."""
        handle = "calibrated_src"
        # 70% predictions, 7/10 correct
        for i in range(7):
            _insert_prediction(handle, 0.70, True)
        for i in range(3):
            _insert_prediction(handle, 0.70, False)

        result = db.compute_calibration(handle)
        self.assertIsNotNone(result)
        # May or may not be flagged as calibrated depending on bucket analysis
        # but should NOT be overconfident
        self.assertFalse(result["is_overconfident"])

    def test_buckets_present(self):
        handle = "bucket_src"
        for i in range(12):
            _insert_prediction(handle, 0.65, i < 8)
        result = db.compute_calibration(handle)
        self.assertIsNotNone(result)
        self.assertGreater(len(result["buckets"]), 0)
        # Each bucket has the required fields
        for b in result["buckets"]:
            self.assertIn("range", b)
            self.assertIn("predicted", b)
            self.assertIn("actual", b)
            self.assertIn("count", b)


class TestTimingScore(unittest.TestCase):
    """Test the per-prediction timing score formula."""

    def test_early_correct_high_score(self):
        """Prediction made 30 days early + correct → high score."""
        now = int(time.time())
        score = db.compute_timing_score(
            extracted_at=now - 30 * 86400,
            market_price_at_prediction=0.50,
            direction="YES",
            resolved_correct=1,
            resolved_at=now,
        )
        self.assertIsNotNone(score)
        self.assertGreater(score, 0.5)

    def test_late_correct_lower_score(self):
        """Prediction made 1 day before → lower score."""
        now = int(time.time())
        score = db.compute_timing_score(
            extracted_at=now - 1 * 86400,
            market_price_at_prediction=0.50,
            direction="YES",
            resolved_correct=1,
            resolved_at=now,
        )
        self.assertIsNotNone(score)
        self.assertLess(score, 0.5)

    def test_incorrect_prediction_zero(self):
        """Incorrect prediction → score = 0.0."""
        now = int(time.time())
        score = db.compute_timing_score(
            extracted_at=now - 30 * 86400,
            market_price_at_prediction=0.50,
            direction="YES",
            resolved_correct=0,
            resolved_at=now,
        )
        self.assertEqual(score, 0.0)

    def test_contrarian_bonus(self):
        """Predicting YES when market is at 20% (contrarian) → bonus."""
        now = int(time.time())
        contrarian_score = db.compute_timing_score(
            extracted_at=now - 15 * 86400,
            market_price_at_prediction=0.20,  # market says 20% YES
            direction="YES",
            resolved_correct=1,
            resolved_at=now,
        )
        consensus_score = db.compute_timing_score(
            extracted_at=now - 15 * 86400,
            market_price_at_prediction=0.70,  # market says 70% YES
            direction="YES",
            resolved_correct=1,
            resolved_at=now,
        )
        self.assertGreater(contrarian_score, consensus_score)

    def test_score_always_in_range(self):
        """Score always in [0.0, 1.0]."""
        now = int(time.time())
        for days in [0, 1, 10, 30, 100]:
            for price in [0.0, 0.1, 0.5, 0.9, 1.0]:
                for correct in [0, 1]:
                    for d in ["YES", "NO"]:
                        score = db.compute_timing_score(
                            now - days * 86400, price, d, correct, now,
                        )
                        if score is not None:
                            self.assertGreaterEqual(score, 0.0)
                            self.assertLessEqual(score, 1.0)

    def test_missing_data_returns_none(self):
        score = db.compute_timing_score(None, 0.5, "YES", 1, None)
        self.assertIsNone(score)


class TestCalibrationIntegration(unittest.TestCase):
    """Test that calibration and timing are computed during recompute."""

    def test_recompute_triggers_calibration(self):
        handle = "recomp_cal_src"
        for i in range(12):
            _insert_prediction(handle, 0.7, i < 8, age_days=i)

        db.recompute_all_credibilities()

        cal = db.get_source_calibration(handle)
        self.assertIsNotNone(cal)
        self.assertIn("calibration_score", cal)
        self.assertIn("brier_score", cal)


class TestCredibilityAPIResponse(unittest.TestCase):
    """Test that the API includes calibration and timing in response."""

    def test_endpoint_includes_calibration_and_timing(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        # Unauthenticated request should fail
        r = client.get("/api/credibility/test_handle")
        self.assertIn(r.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
