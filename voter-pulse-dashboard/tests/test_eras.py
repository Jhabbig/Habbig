"""Tests for the per-administration era slicing."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import eras  # noqa: E402


def _series(points):
    return {"series_id": "X", "label": "X", "group": "g", "units": "%",
            "higher_is_better": False, "points": points, "latest": points[-1] if points else None}


class ErasTests(unittest.TestCase):
    def test_eras_table_covers_modern_presidents(self):
        labels = [e[0] for e in eras.ERAS]
        for name in ("Reagan", "Clinton", "Obama", "Trump I", "Biden", "Trump II"):
            self.assertIn(name, labels)

    def test_slice_series_inclusive_start_exclusive_end(self):
        points = [
            {"date": "1981-01-01", "value": 1.0},  # just before Reagan inauguration
            {"date": "1981-02-01", "value": 2.0},
            {"date": "1988-12-01", "value": 99.0},
            {"date": "1989-01-20", "value": 100.0},  # day Bush Sr is inaugurated
        ]
        # Reagan window: 1981-01-20 .. 1989-01-20 (exclusive)
        vals = eras.slice_series(points, "1981-01-20", "1989-01-20")
        # 1981-01-01 < start → excluded; 1989-01-20 == end → excluded
        self.assertEqual(vals, [2.0, 99.0])

    def test_slice_series_open_ended(self):
        points = [
            {"date": "2024-12-01", "value": 1.0},  # before Trump II
            {"date": "2025-01-19", "value": 2.0},  # day before Trump II
            {"date": "2025-02-01", "value": 3.0},  # Trump II
            {"date": "2026-01-01", "value": 4.0},  # Trump II
        ]
        vals = eras.slice_series(points, "2025-01-20", None)
        self.assertEqual(vals, [3.0, 4.0])

    def test_stats_for_series_computes_mean_min_max(self):
        # one point per era is enough to verify the n_points wiring
        points = [
            {"date": "1981-06-01", "value": 5.0},   # Reagan
            {"date": "1985-06-01", "value": 7.0},   # Reagan
            {"date": "1993-06-01", "value": 11.0},  # Clinton
        ]
        rows = eras.stats_for_series({"points": points})
        reagan = next(r for r in rows if r["label"] == "Reagan")
        self.assertEqual(reagan["n_points"], 2)
        self.assertAlmostEqual(reagan["mean"], 6.0, places=2)
        self.assertEqual(reagan["min"], 5.0)
        self.assertEqual(reagan["max"], 7.0)
        # An era with no data should still appear with None
        bushjr = next(r for r in rows if r["label"] == "Bush Jr.")
        self.assertEqual(bushjr["n_points"], 0)
        self.assertIsNone(bushjr["mean"])

    def test_compose_filters_to_requested_series(self):
        rows = [{
            "series_id": "CPIAUCSL", "label": "CPI", "group": "p", "units": "i",
            "higher_is_better": False,
            "points": [{"date": "1985-06-01", "value": 100.0}],
            "latest": {"date": "1985-06-01", "value": 100.0},
        }]
        out = eras.compose(rows, ["CPIAUCSL", "MISSING"])
        self.assertIn("CPIAUCSL", out["series"])
        self.assertNotIn("MISSING", out["series"])


if __name__ == "__main__":
    unittest.main()
