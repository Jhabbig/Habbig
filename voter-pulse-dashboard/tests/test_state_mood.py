"""Tests for the state-level mood proxy + tile layout."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import state_mood  # noqa: E402


class StateMoodTests(unittest.TestCase):
    def test_mood_from_unrate_endpoints(self):
        self.assertEqual(state_mood.mood_from_unrate(3.0), 100.0)
        self.assertAlmostEqual(state_mood.mood_from_unrate(6.0), 50.0, places=1)
        self.assertEqual(state_mood.mood_from_unrate(9.0), 0.0)
        self.assertEqual(state_mood.mood_from_unrate(12.0), 0.0)
        self.assertIsNone(state_mood.mood_from_unrate(None))

    def test_tile_layout_has_50_states_and_dc(self):
        # We expect 50 states + DC = 51 entries.
        self.assertEqual(len(state_mood.TILE_LAYOUT), 51)
        self.assertIn("CA", state_mood.TILE_LAYOUT)
        self.assertIn("DC", state_mood.TILE_LAYOUT)

    def test_annotate_attaches_mood_and_tile(self):
        ann = state_mood.annotate([{
            "postal": "CA", "name": "California", "series_id": "CAUR",
            "region": "West",
            "latest": {"date": "2026-04-01", "value": 5.4},
            "delta_1y_pp": 0.2, "delta_4y_pp": 0.8,
        }])
        self.assertEqual(len(ann), 1)
        row = ann[0]
        self.assertAlmostEqual(row["mood"], 60.0, places=1)  # 5.4% → 60
        self.assertEqual(row["tile"], {"row": 3, "col": 1})

    def test_compose_rankings(self):
        states = [
            {"postal":"FL","name":"Florida","series_id":"FLUR","region":"South",
             "latest":{"value":3.4},"delta_1y_pp":-0.2,"delta_4y_pp":-0.6},
            {"postal":"WV","name":"West Virginia","series_id":"WVUR","region":"South",
             "latest":{"value":6.2},"delta_1y_pp":0.5,"delta_4y_pp":1.5},
            {"postal":"CA","name":"California","series_id":"CAUR","region":"West",
             "latest":{"value":5.4},"delta_1y_pp":0.2,"delta_4y_pp":0.8},
        ]
        out = state_mood.compose({"states": states,
                                   "benchmark": {"n":3,"mean":5.0,"median":5.4,"min":3.4,"max":6.2}})
        # Florida is lowest UR, WV highest
        self.assertEqual(out["rankings"]["lowest_unemployment"][0], "FL")
        self.assertEqual(out["rankings"]["highest_unemployment"][0], "WV")
        # The biggest 1-year improvement is the most-negative pp delta (Florida)
        self.assertEqual(out["rankings"]["biggest_1y_improvers"][0], "FL")


if __name__ == "__main__":
    unittest.main()
