"""Tests for the historical-mood replay (series_as_of)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import historical_mood  # noqa: E402


def _ser(sid, group, points, *, higher_is_better=False, units="%"):
    return {"series_id": sid, "label": sid, "group": group, "units": units,
            "higher_is_better": higher_is_better, "points": points,
            "latest": points[-1] if points else None, "yoy_pct": None}


class HistoricalMoodTests(unittest.TestCase):
    def test_series_as_of_truncates_to_target(self):
        points = [{"date": f"202{y}-01-01", "value": float(y)} for y in range(0, 6)]
        ser = _ser("X", "pocketbook", points)
        sliced = historical_mood.series_as_of(ser, "2022-06-01")
        # Should keep observations on-or-before 2022-06-01
        self.assertIsNotNone(sliced)
        self.assertEqual(len(sliced["points"]), 3)  # 2020, 2021, 2022
        self.assertEqual(sliced["latest"]["date"], "2022-01-01")

    def test_series_as_of_recomputes_yoy(self):
        points = [
            {"date": "2024-01-01", "value": 100.0},
            {"date": "2025-01-01", "value": 105.0},
        ]
        ser = _ser("X", "pocketbook", points)
        sliced = historical_mood.series_as_of(ser, "2025-06-01")
        self.assertAlmostEqual(sliced["yoy_pct"], 5.0, places=2)

    def test_series_as_of_returns_none_when_no_data(self):
        ser = _ser("X", "pocketbook", [{"date": "2030-01-01", "value": 1.0}])
        self.assertIsNone(historical_mood.series_as_of(ser, "2020-01-01"))

    def test_mood_as_of_runs_compose_against_truncated_rows(self):
        rows = [
            _ser("CPIAUCSL", "pocketbook", [
                {"date": "2024-01-01", "value": 100.0},
                {"date": "2025-01-01", "value": 103.0},
            ]),
            _ser("UNRATE", "jobs", [
                {"date": "2024-01-01", "value": 4.0},
                {"date": "2025-01-01", "value": 4.5},
            ]),
            _ser("UMCSENT", "sentiment", [
                {"date": "2024-01-01", "value": 70.0},
                {"date": "2025-01-01", "value": 65.0},
            ], higher_is_better=True),
        ]
        out = historical_mood.mood_as_of(rows, "2025-06-01")
        self.assertIn("overall", out)
        self.assertIn("subscores", out)
        self.assertEqual(out["as_of"], "2025-06-01")

    def test_monthly_history_walks_every_month(self):
        rows = [
            _ser("UNRATE", "jobs", [
                {"date": "2024-01-01", "value": 4.0},
                {"date": "2024-02-01", "value": 4.1},
                {"date": "2024-03-01", "value": 4.2},
            ]),
        ]
        hist = historical_mood.monthly_history(rows, start="2024-01-01")
        self.assertEqual(len(hist), 3)
        self.assertEqual(hist[0]["date"], "2024-01-01")
        self.assertEqual(hist[-1]["date"], "2024-03-01")


if __name__ == "__main__":
    unittest.main()
