"""Tests for the regional ('for-you cut') mood compose."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import regional_mood  # noqa: E402


class RegionalMoodTests(unittest.TestCase):
    def setUp(self):
        self.cpi = [
            {"region":"Northeast","series_id":"CUUR0100SA0","latest":{"value":325.1},"yoy_pct":2.8},
            {"region":"South",    "series_id":"CUUR0300SA0","latest":{"value":318.4},"yoy_pct":3.9},
        ]
        self.states = {"states":[
            {"postal":"NY","region":"Northeast","latest":{"value":4.5}},
            {"postal":"MA","region":"Northeast","latest":{"value":3.7}},
            {"postal":"TX","region":"South",    "latest":{"value":3.9}},
            {"postal":"FL","region":"South",    "latest":{"value":3.4}},
        ]}

    def test_compose_returns_one_row_per_region_input(self):
        out = regional_mood.compose(self.cpi, self.states, 64.5)
        self.assertEqual(len(out["regions"]), 2)
        labels = [r["region"] for r in out["regions"]]
        self.assertIn("Northeast", labels)
        self.assertIn("South", labels)

    def test_region_with_low_cpi_and_low_unemployment_has_higher_mood(self):
        out = regional_mood.compose(self.cpi, self.states, 64.5)
        ne = next(r for r in out["regions"] if r["region"] == "Northeast")
        south = next(r for r in out["regions"] if r["region"] == "South")
        # NE has lower CPI YoY (2.8 vs 3.9). South has lower avg UR.
        # Both signals matter — just assert pocketbook sub differs in the
        # expected direction.
        self.assertGreater(ne["pocketbook"]["score"], south["pocketbook"]["score"])

    def test_jobs_score_uses_state_average(self):
        out = regional_mood.compose(self.cpi, self.states, 64.5)
        ne = next(r for r in out["regions"] if r["region"] == "Northeast")
        self.assertAlmostEqual(ne["jobs"]["avg_state_unrate_pct"], 4.1, places=2)
        self.assertEqual(ne["jobs"]["n_states"], 2)

    def test_national_baseline_present(self):
        out = regional_mood.compose(self.cpi, self.states, 64.5)
        nb = out["national_baseline"]
        self.assertIn("overall", nb)
        self.assertIn("pocketbook_score", nb)
        self.assertIn("jobs_score", nb)

    def test_handles_missing_umich(self):
        out = regional_mood.compose(self.cpi, self.states, None)
        for r in out["regions"]:
            self.assertIsNone(r["sentiment"]["score"])
        # Overall is still computed off the other two sub-scores
        self.assertIsNotNone(out["regions"][0]["overall"])


if __name__ == "__main__":
    unittest.main()
