"""Tests for the CSV-export serializers."""
from __future__ import annotations

import csv
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import exports  # noqa: E402


class ExportsTests(unittest.TestCase):
    def _parse(self, payload):
        body, ctype = payload
        self.assertIn("text/csv", ctype)
        rows = list(csv.reader(io.StringIO(body)))
        return rows

    def test_mood_history_csv_has_header_and_rows(self):
        history = [
            {"date": "2024-01-01", "overall": 48.5, "misery_index": 7.1},
            {"date": "2024-02-01", "overall": 47.2, "misery_index": 7.4},
        ]
        rows = self._parse(exports.mood_history_csv(history))
        self.assertEqual(rows[0], ["date", "mood_overall", "misery_index"])
        self.assertEqual(rows[1], ["2024-01-01", "48.5", "7.1"])
        self.assertEqual(len(rows), 3)

    def test_mood_history_csv_handles_empty(self):
        rows = self._parse(exports.mood_history_csv([]))
        self.assertEqual(rows, [["date", "mood_overall", "misery_index"]])

    def test_mood_history_csv_handles_missing_values(self):
        history = [{"date": "2024-01-01", "overall": None, "misery_index": None}]
        rows = self._parse(exports.mood_history_csv(history))
        self.assertEqual(rows[1], ["2024-01-01", "", ""])

    def test_life_long_csv_pivots_each_point(self):
        series = [{
            "series_id": "UNRATE", "label": "Unemployment", "units": "%",
            "points": [
                {"date": "2024-01-01", "value": 4.0},
                {"date": "2024-02-01", "value": 4.1},
            ],
        }]
        rows = self._parse(exports.life_long_csv(series))
        self.assertEqual(rows[0], ["series_id", "label", "units", "date", "value"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][0], "UNRATE")
        self.assertEqual(rows[1][3], "2024-01-01")

    def test_states_csv_rows(self):
        states = [{
            "postal": "CA", "name": "California", "region": "West",
            "latest": {"date": "2026-04-01", "value": 5.4},
            "delta_1y_pp": 0.2, "delta_4y_pp": 0.8, "mood": 60.0,
        }]
        rows = self._parse(exports.states_csv(states))
        self.assertEqual(rows[0][0], "postal")
        self.assertEqual(rows[1][0], "CA")
        self.assertEqual(rows[1][3], "2026-04-01")
        self.assertEqual(rows[1][4], "5.4")

    def test_world_csv_rows(self):
        countries = [{
            "iso3": "DEU", "name": "Germany", "year": 2022,
            "agriculture_pct": 1.2, "industry_pct": 27.0, "services_pct": 71.8,
            "gdp_per_capita_usd": 48430.0,
            "bucket": "post_industrial", "stage": 4,
        }]
        rows = self._parse(exports.world_csv(countries))
        self.assertEqual(rows[0][0], "iso3")
        self.assertEqual(rows[1][0], "DEU")
        self.assertEqual(rows[1][7], "post_industrial")
        self.assertEqual(rows[1][8], "4")


if __name__ == "__main__":
    unittest.main()
