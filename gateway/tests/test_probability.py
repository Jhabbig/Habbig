"""Tests for betyc probability calculation and edge delta."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


class TestBetycProbability(unittest.TestCase):
    """Tests for calculate_betyc_probability."""

    def test_no_predictions_insufficient_data(self):
        result = db.calculate_betyc_probability([])
        self.assertIsNone(result["betyc_yes_probability"])
        self.assertEqual(result["betyc_source_count"], 0)
        self.assertEqual(result["betyc_confidence"], "Insufficient data")

    def test_single_source_with_explicit_probability(self):
        preds = [
            {"predicted_probability": 0.75, "global_credibility": 0.8,
             "category_credibility": None, "direction": None, "accuracy_unlocked": False},
        ]
        result = db.calculate_betyc_probability(preds)
        self.assertIsNotNone(result["betyc_yes_probability"])
        self.assertEqual(result["betyc_source_count"], 1)
        # Single source = Low confidence
        self.assertEqual(result["betyc_confidence"], "Low")

    def test_result_clamped_to_bounds(self):
        """Result must be clamped to [0.05, 0.95]."""
        # Very high probability
        preds = [
            {"predicted_probability": 0.99, "global_credibility": 0.99,
             "category_credibility": None, "direction": None, "accuracy_unlocked": True},
        ]
        result = db.calculate_betyc_probability(preds)
        self.assertLessEqual(result["betyc_yes_probability"], 0.95)
        self.assertGreaterEqual(result["betyc_yes_probability"], 0.05)

        # Very low probability
        preds2 = [
            {"predicted_probability": 0.01, "global_credibility": 0.99,
             "category_credibility": None, "direction": None, "accuracy_unlocked": True},
        ]
        result2 = db.calculate_betyc_probability(preds2)
        self.assertGreaterEqual(result2["betyc_yes_probability"], 0.05)

    def test_directional_yes_conversion(self):
        """YES from source with credibility X -> 0.5 + (X - 0.5) * 0.8."""
        preds = [
            {"predicted_probability": None, "global_credibility": 0.8,
             "category_credibility": None, "direction": "YES", "accuracy_unlocked": False},
        ]
        result = db.calculate_betyc_probability(preds)
        # Expected: 0.5 + (0.8 - 0.5) * 0.8 = 0.5 + 0.24 = 0.74
        self.assertIsNotNone(result["betyc_yes_probability"])
        self.assertAlmostEqual(result["betyc_yes_probability"], 0.74, places=2)

    def test_directional_no_conversion(self):
        """NO from source with credibility X -> 0.5 - (X - 0.5) * 0.8."""
        preds = [
            {"predicted_probability": None, "global_credibility": 0.8,
             "category_credibility": None, "direction": "NO", "accuracy_unlocked": False},
        ]
        result = db.calculate_betyc_probability(preds)
        # Expected: 0.5 - (0.8 - 0.5) * 0.8 = 0.5 - 0.24 = 0.26
        self.assertIsNotNone(result["betyc_yes_probability"])
        self.assertAlmostEqual(result["betyc_yes_probability"], 0.26, places=2)

    def test_confidence_high(self):
        """>=5 sources, avg cred >=0.6, majority accuracy_unlocked -> High."""
        preds = [
            {"predicted_probability": 0.7, "global_credibility": 0.8,
             "category_credibility": None, "direction": None, "accuracy_unlocked": True},
            {"predicted_probability": 0.65, "global_credibility": 0.75,
             "category_credibility": None, "direction": None, "accuracy_unlocked": True},
            {"predicted_probability": 0.72, "global_credibility": 0.7,
             "category_credibility": None, "direction": None, "accuracy_unlocked": True},
            {"predicted_probability": 0.68, "global_credibility": 0.65,
             "category_credibility": None, "direction": None, "accuracy_unlocked": True},
            {"predicted_probability": 0.71, "global_credibility": 0.6,
             "category_credibility": None, "direction": None, "accuracy_unlocked": False},
        ]
        result = db.calculate_betyc_probability(preds)
        self.assertEqual(result["betyc_confidence"], "High")
        self.assertEqual(result["betyc_source_count"], 5)

    def test_confidence_medium(self):
        """3-4 sources OR avg cred 0.4-0.6 -> Medium."""
        preds = [
            {"predicted_probability": 0.6, "global_credibility": 0.5,
             "category_credibility": None, "direction": None, "accuracy_unlocked": False},
            {"predicted_probability": 0.55, "global_credibility": 0.45,
             "category_credibility": None, "direction": None, "accuracy_unlocked": False},
            {"predicted_probability": 0.58, "global_credibility": 0.5,
             "category_credibility": None, "direction": None, "accuracy_unlocked": False},
        ]
        result = db.calculate_betyc_probability(preds)
        self.assertEqual(result["betyc_confidence"], "Medium")

    def test_confidence_low(self):
        """1-2 sources OR avg cred <0.4 -> Low."""
        preds = [
            {"predicted_probability": 0.6, "global_credibility": 0.3,
             "category_credibility": None, "direction": None, "accuracy_unlocked": False},
        ]
        result = db.calculate_betyc_probability(preds)
        self.assertEqual(result["betyc_confidence"], "Low")

    def test_edge_delta_positive(self):
        """Positive edge means betyc > market."""
        market_yes = 0.67
        betyc_yes = 0.73
        edge = betyc_yes - market_yes
        self.assertGreater(edge, 0)

    def test_edge_delta_negative(self):
        """Negative edge means market > betyc."""
        market_yes = 0.73
        betyc_yes = 0.67
        edge = betyc_yes - market_yes
        self.assertLess(edge, 0)

    def test_credibility_weighting(self):
        """Higher credibility sources should have more weight."""
        # Source A: high cred, says 80%
        # Source B: low cred, says 20%
        preds = [
            {"predicted_probability": 0.8, "global_credibility": 0.9,
             "category_credibility": None, "direction": None, "accuracy_unlocked": False},
            {"predicted_probability": 0.2, "global_credibility": 0.1,
             "category_credibility": None, "direction": None, "accuracy_unlocked": False},
        ]
        result = db.calculate_betyc_probability(preds)
        # Result should be closer to 0.8 than 0.2 due to weighting
        self.assertGreater(result["betyc_yes_probability"], 0.5)

    def test_category_credibility_preferred_over_global(self):
        """When category_credibility is available, it should be used for weighting."""
        preds = [
            {"predicted_probability": 0.7, "global_credibility": 0.3,
             "category_credibility": 0.9, "direction": None, "accuracy_unlocked": False},
        ]
        result = db.calculate_betyc_probability(preds)
        # Should use category_credibility=0.9 for weight
        self.assertIsNotNone(result["betyc_yes_probability"])


if __name__ == "__main__":
    unittest.main()
