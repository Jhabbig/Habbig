"""Tests for the Clark-Fisher classifier and ternary math."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import clark_fisher as cf  # noqa: E402


class ClarkFisherTests(unittest.TestCase):
    def test_ternary_vertices(self):
        # Pure agriculture → top of triangle
        t = cf.ternary_xy(100, 0, 0)
        self.assertAlmostEqual(t["x"], 0.5, places=3)
        self.assertAlmostEqual(t["y"], math.sqrt(3) / 2, places=3)
        # Pure industry → bottom-left
        t = cf.ternary_xy(0, 100, 0)
        self.assertAlmostEqual(t["x"], 0.0, places=3)
        self.assertAlmostEqual(t["y"], 0.0, places=3)
        # Pure services → bottom-right
        t = cf.ternary_xy(0, 0, 100)
        self.assertAlmostEqual(t["x"], 1.0, places=3)
        self.assertAlmostEqual(t["y"], 0.0, places=3)

    def test_ternary_centroid(self):
        # Equal split → centroid at (0.5, sqrt(3)/6 ≈ 0.289)
        t = cf.ternary_xy(33.33, 33.33, 33.33)
        self.assertAlmostEqual(t["x"], 0.5, places=3)
        self.assertAlmostEqual(t["y"], math.sqrt(3) / 6, places=3)

    def test_classify_known_country_shapes(self):
        # Ethiopia-ish — 65% agri → pre-industrial
        self.assertEqual(cf.classify(65, 10, 25)["bucket"], "pre_industrial")
        # India-ish — 43% agri → pre-industrial (just over the cutoff)
        self.assertEqual(cf.classify(43, 25, 32)["bucket"], "pre_industrial")
        # Vietnam-ish — 28% agri → industrialising
        self.assertEqual(cf.classify(28, 33, 39)["bucket"], "industrialising")
        # Mexico-ish — 12% agri, 63% services → post-industrial
        self.assertEqual(cf.classify(12, 25, 63)["bucket"], "post_industrial")
        # USA-ish — 79% services → information
        self.assertEqual(cf.classify(1.4, 19.5, 79.1)["bucket"], "information")
        # Germany-ish — 71% services → post-industrial
        self.assertEqual(cf.classify(1.2, 27, 71.8)["bucket"], "post_industrial")

    def test_classify_industrial_band(self):
        # Industry-dominant, services < 60 → industrial bucket
        self.assertEqual(cf.classify(5, 40, 55)["bucket"], "industrial")

    def test_development_index_orders_correctly(self):
        # Pre-industrial < industrialising < post-industrial < information
        eth = cf.classify(65, 10, 25)["development_index"]
        ind = cf.classify(28, 33, 39)["development_index"]
        ger = cf.classify(1.2, 27, 71.8)["development_index"]
        usa = cf.classify(1.4, 19.5, 79.1)["development_index"]
        self.assertLess(eth, ind)
        self.assertLess(ind, ger)
        # Germany and USA can both be clipped to 100; just assert ordering
        self.assertLessEqual(ger, usa)

    def test_annotate_attaches_classification_and_ternary(self):
        out = cf.annotate({
            "iso3": "USA", "name": "USA", "year": 2022,
            "agriculture_pct": 1.4, "industry_pct": 19.5, "services_pct": 79.1,
        })
        self.assertEqual(out["bucket"], "information")
        self.assertEqual(out["stage"], 5)
        self.assertIn("x", out["ternary"])
        self.assertIn("y", out["ternary"])

    def test_summarise_counts_per_bucket(self):
        countries = [
            {"iso3":"USA","name":"USA","year":2022,"agriculture_pct":1.4,"industry_pct":19.5,"services_pct":79.1},
            {"iso3":"DEU","name":"DEU","year":2022,"agriculture_pct":1.2,"industry_pct":27.0,"services_pct":71.8},
            {"iso3":"ETH","name":"ETH","year":2022,"agriculture_pct":65.0,"industry_pct":10.0,"services_pct":25.0},
        ]
        out = cf.summarise(countries)
        self.assertEqual(out["n_countries"], 3)
        self.assertEqual(out["stage_counts"]["information"], 1)
        self.assertEqual(out["stage_counts"]["post_industrial"], 1)
        self.assertEqual(out["stage_counts"]["pre_industrial"], 1)


if __name__ == "__main__":
    unittest.main()
