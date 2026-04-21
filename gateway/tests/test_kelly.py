"""Kelly calculator + sizing-table tests."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from portfolio.kelly import kelly_fraction, sizing_table  # noqa: E402


class TestKellyFraction(unittest.TestCase):
    def test_no_edge(self):
        self.assertEqual(kelly_fraction(0.5, 0.5), 0.0)

    def test_negative_edge(self):
        self.assertEqual(kelly_fraction(0.4, 0.5), 0.0)

    def test_classic_positive_edge(self):
        # 60% our prob vs 50% market → raw Kelly = (0.6*1 - 0.4)/1 = 0.2.
        self.assertAlmostEqual(
            kelly_fraction(0.6, 0.5, max_cap=1.0), 0.2, places=6,
        )

    def test_cap_applied(self):
        # Full-Kelly here would be huge (market_prob very low, our_prob
        # close to 1). Cap at 0.25.
        self.assertAlmostEqual(
            kelly_fraction(0.9, 0.1), 0.25, places=6,
        )

    def test_degenerate_inputs(self):
        self.assertEqual(kelly_fraction(0, 0.5), 0.0)
        self.assertEqual(kelly_fraction(0.5, 0), 0.0)
        self.assertEqual(kelly_fraction(1, 0.5), 0.0)
        self.assertEqual(kelly_fraction(0.5, 1), 0.0)


class TestSizingTable(unittest.TestCase):
    def test_zero_bankroll(self):
        t = sizing_table(0.6, 0.5, 0)
        self.assertEqual(t["full"]["stake_usd"], 0.0)
        self.assertEqual(t["half"]["stake_usd"], 0.0)

    def test_positive_edge_sizes(self):
        t = sizing_table(0.6, 0.5, 10_000, max_cap=1.0)
        self.assertGreater(t["full"]["stake_usd"], 0)
        self.assertGreater(t["half"]["stake_usd"], 0)
        self.assertGreater(t["quarter"]["stake_usd"], 0)
        self.assertGreater(t["full"]["stake_usd"], t["half"]["stake_usd"])
        self.assertGreater(t["half"]["stake_usd"], t["quarter"]["stake_usd"])

    def test_max_loss_equals_stake(self):
        t = sizing_table(0.6, 0.5, 10_000, max_cap=1.0)
        self.assertAlmostEqual(
            t["full"]["max_loss_usd"], t["full"]["stake_usd"], places=2,
        )

    def test_max_profit_matches_odds(self):
        # At 50% market, b = 1, so max_profit == stake for full Kelly.
        t = sizing_table(0.6, 0.5, 10_000, max_cap=1.0)
        self.assertAlmostEqual(
            t["full"]["max_profit_usd"], t["full"]["stake_usd"], places=2,
        )

    def test_edge_pct(self):
        t = sizing_table(0.6, 0.5, 1_000)
        self.assertAlmostEqual(t["edge_pct"], 10.0, places=2)


if __name__ == "__main__":
    unittest.main()
