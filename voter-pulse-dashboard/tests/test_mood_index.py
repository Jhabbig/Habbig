"""Tests for the composite mood index and its sub-scores."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import mood_index  # noqa: E402


def _series(sid: str, value: float, *, yoy_pct: float | None = None,
            group: str = "pocketbook", higher_is_better: bool = False) -> dict:
    return {
        "series_id": sid, "label": sid, "group": group, "units": "x",
        "higher_is_better": higher_is_better,
        "latest": {"date": "2025-01-01", "value": value},
        "yoy_pct": yoy_pct, "points": [{"date": "2025-01-01", "value": value}],
    }


class MoodIndexTests(unittest.TestCase):
    def test_label_for_buckets(self):
        self.assertEqual(mood_index.label_for(None), "n/a")
        self.assertEqual(mood_index.label_for(80), "Good")
        self.assertEqual(mood_index.label_for(60), "Okay")
        self.assertEqual(mood_index.label_for(45), "Strained")
        self.assertEqual(mood_index.label_for(30), "Sour")
        self.assertEqual(mood_index.label_for(10), "Bleak")

    def test_pocketbook_score_zero_cpi_is_100(self):
        rows = [_series("CPIAUCSL", 100, yoy_pct=0.0)]
        sub = mood_index.pocketbook_score(rows)
        self.assertEqual(sub.value, 100)

    def test_pocketbook_score_four_pct_cpi_is_fifty(self):
        rows = [_series("CPIAUCSL", 100, yoy_pct=4.0)]
        sub = mood_index.pocketbook_score(rows)
        self.assertAlmostEqual(sub.value, 50.0, places=1)

    def test_pocketbook_score_eight_pct_cpi_floors_at_zero(self):
        rows = [_series("CPIAUCSL", 100, yoy_pct=8.0)]
        sub = mood_index.pocketbook_score(rows)
        self.assertEqual(sub.value, 0.0)

    def test_jobs_score_low_unemployment(self):
        rows = [_series("UNRATE", 3.0, group="jobs")]
        sub = mood_index.jobs_score(rows)
        self.assertAlmostEqual(sub.value, 100.0, places=1)

    def test_jobs_score_six_pct_unemployment_is_fifty(self):
        rows = [_series("UNRATE", 6.0, group="jobs")]
        sub = mood_index.jobs_score(rows)
        self.assertAlmostEqual(sub.value, 50.0, places=1)

    def test_sentiment_score_uses_long_run_range(self):
        rows = [_series("UMCSENT", 110, group="sentiment", higher_is_better=True)]
        sub = mood_index.sentiment_score(rows)
        self.assertAlmostEqual(sub.value, 100.0, places=1)
        rows = [_series("UMCSENT", 50, group="sentiment", higher_is_better=True)]
        sub = mood_index.sentiment_score(rows)
        self.assertAlmostEqual(sub.value, 0.0, places=1)

    def test_misery_index_sums_unrate_plus_cpi_yoy(self):
        rows = [
            _series("UNRATE", 4.0, group="jobs"),
            _series("CPIAUCSL", 100, yoy_pct=3.5),
        ]
        self.assertAlmostEqual(mood_index.misery_index(rows), 7.5, places=2)

    def test_expectations_gap(self):
        rows = [
            _series("MICH", 4.5, group="sentiment"),
            _series("CPIAUCSL", 100, yoy_pct=3.0),
        ]
        self.assertAlmostEqual(mood_index.expectations_gap(rows), 1.5, places=2)

    def test_compose_returns_overall_and_subscores(self):
        rows = [
            _series("CPIAUCSL", 100, yoy_pct=3.0),
            _series("UNRATE",  4.0, group="jobs"),
            _series("UMCSENT", 80, group="sentiment", higher_is_better=True),
        ]
        out = mood_index.compose(rows)
        self.assertIn("overall", out)
        self.assertIn("subscores", out)
        self.assertIn("pocketbook", out["subscores"])
        self.assertIn("jobs", out["subscores"])
        self.assertIn("sentiment", out["subscores"])
        self.assertGreater(out["overall"], 0)
        self.assertLess(out["overall"], 100)

    def test_compose_handles_missing_subscores_gracefully(self):
        out = mood_index.compose([])
        self.assertIsNone(out["overall"])
        self.assertIsNone(out["misery_index"])
        self.assertIsNone(out["expectations_gap"])


if __name__ == "__main__":
    unittest.main()
