"""Tests for the election backtest math."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import elections  # noqa: E402


class ElectionsTests(unittest.TestCase):
    def test_shift_months_subtracts_whole_months(self):
        self.assertEqual(elections._shift_months("2024-11-05", 6), "2024-05-01")
        self.assertEqual(elections._shift_months("2024-11-05", 12), "2023-11-01")
        self.assertEqual(elections._shift_months("2024-01-15", 1), "2023-12-01")
        self.assertEqual(elections._shift_months("2024-11-05", 0), "2024-11-01")

    def test_pearson_returns_correct_sign(self):
        # Perfectly positive
        r = elections._pearson([1, 2, 3, 4], [2, 4, 6, 8])
        self.assertAlmostEqual(r, 1.0, places=3)
        # Perfectly negative
        r = elections._pearson([1, 2, 3, 4], [8, 6, 4, 2])
        self.assertAlmostEqual(r, -1.0, places=3)
        # Too few points
        self.assertIsNone(elections._pearson([1, 2], [1, 2]))

    def test_lookup_mood_returns_latest_on_or_before(self):
        history = [
            {"date": "2024-05-01", "overall": 50.0},
            {"date": "2024-06-01", "overall": 55.0},
            {"date": "2024-07-01", "overall": 52.0},
        ]
        self.assertEqual(elections._lookup_mood(history, "2024-06-15"), 55.0)
        self.assertEqual(elections._lookup_mood(history, "2024-04-01"), None)
        self.assertEqual(elections._lookup_mood(history, "2024-07-01"), 52.0)

    def test_elections_table_contains_known_entries(self):
        years = [e["year"] for e in elections.ELECTIONS]
        for y in (1980, 1992, 2008, 2016, 2020, 2024):
            self.assertIn(y, years)

    def test_run_returns_per_horizon_summary(self):
        # Minimal synthetic FRED-shaped data; mood will mostly be null but
        # the function should still return the expected structure.
        rows = []
        out = elections.run(rows)
        self.assertIn("elections", out)
        self.assertIn("by_horizon", out)
        self.assertIn("horizons_months", out)
        self.assertIn("history", out)
        # by_horizon should have a key per horizon
        for h in elections.HORIZONS_MONTHS:
            self.assertIn(f"h{h}mo", out["by_horizon"])


if __name__ == "__main__":
    unittest.main()
