"""Unit tests for the intelligence-layer pure-function modules.

Covers:
  - credibility/calibration (Brier + reliability diagram)
  - credibility/timing      (timing score with edge cases)
  - credibility/network     (pairwise + classify + clusters + consensus)
  - backtest                (kelly + simulate + sharpe + drawdown)
  - insider.score           (compute_insider_score)
  - ai.environmental        (unit conversions)

No DB, no network, no Claude SDK. Fast.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from credibility import calibration, timing, network
import backtest as bt
from insider.score import compute_insider_score
from ai import environmental as env


# ── Calibration ─────────────────────────────────────────────────────────────


class TestBrierScore(unittest.TestCase):
    def test_insufficient_sample_returns_none(self):
        recs = [{"predicted_probability_stated": 0.7, "resolved_correct": 1}] * 9
        self.assertIsNone(calibration.compute_brier_score(recs))

    def test_perfect_calibration_is_one(self):
        recs = [{"predicted_probability_stated": 1.0, "resolved_correct": 1}] * 10
        result = calibration.compute_brier_score(recs)
        self.assertIsNotNone(result)
        self.assertEqual(result["calibration"], 1.0)
        self.assertEqual(result["brier"], 0.0)

    def test_anti_calibrated_is_low(self):
        # Said 100%, all wrong → worst possible.
        recs = [{"predicted_probability_stated": 1.0, "resolved_correct": 0}] * 10
        result = calibration.compute_brier_score(recs)
        self.assertIsNotNone(result)
        self.assertEqual(result["brier"], 1.0)
        # 1 - 1.0/0.25 = -3, clamped to 0.
        self.assertEqual(result["calibration"], 0.0)

    def test_skips_records_without_probability(self):
        recs = [
            {"predicted_probability_stated": None, "resolved_correct": 1},
            {"predicted_probability_stated": 0.7, "resolved_correct": 1},
        ] * 5
        result = calibration.compute_brier_score(recs)
        # Only 5 usable records → below MIN_SAMPLE.
        self.assertIsNone(result)

    def test_reliability_diagram_flags_overconfidence(self):
        # Bucket predictions at 0.9 but actual outcome only 50% correct → overconfident.
        recs = [{"predicted_probability_stated": 0.9, "resolved_correct": i % 2}
                for i in range(10)]
        buckets = calibration.reliability_diagram_data(recs, bins=10)
        last_bucket = buckets[-1]  # 0.9-1.0 range
        self.assertEqual(last_bucket["count"], 10)
        self.assertTrue(last_bucket["is_overconfident"])
        self.assertFalse(last_bucket["is_underconfident"])


# ── Timing ──────────────────────────────────────────────────────────────────


class TestTimingScore(unittest.TestCase):
    def test_correct_contrarian_early_gets_high_score(self):
        # 30 days out, market says 20%, we said YES and were correct.
        result = timing.compute_timing_score(
            predicted_at=0,
            market_close_time=30 * 86400,
            market_implied_at_prediction=0.20,
            resolved_correct=1,
            predicted_direction="YES",
        )
        self.assertGreater(result["timing_score"], 0.8)
        # Edge = 0.5 - 0.20 = 0.30
        self.assertAlmostEqual(result["edge_at_prediction"], 0.30, places=3)

    def test_wrong_contrarian_scores_zero(self):
        result = timing.compute_timing_score(
            predicted_at=0,
            market_close_time=30 * 86400,
            market_implied_at_prediction=0.20,
            resolved_correct=0,
            predicted_direction="YES",
        )
        self.assertEqual(result["timing_score"], 0.0)

    def test_late_correct_nonconsensus_still_scores(self):
        # 2 days remaining, market 45% (not contrarian threshold), correct.
        result = timing.compute_timing_score(
            predicted_at=0,
            market_close_time=2 * 86400,
            market_implied_at_prediction=0.45,
            resolved_correct=1,
            predicted_direction="YES",
        )
        self.assertGreater(result["timing_score"], 0.0)
        self.assertLess(result["timing_score"], 0.3)

    def test_unknown_direction_no_contrarian_bonus(self):
        result = timing.compute_timing_score(
            predicted_at=0,
            market_close_time=30 * 86400,
            market_implied_at_prediction=0.1,
            resolved_correct=1,
            predicted_direction="unknown",
        )
        # Time component still counts → score around 0.75 max (time=1 * outcome=1.5 / 2)
        self.assertLessEqual(result["timing_score"], 0.76)


# ── Network ─────────────────────────────────────────────────────────────────


class TestPairwiseStats(unittest.TestCase):
    def test_under_threshold_returns_none(self):
        a = [{"market_slug": f"m{i}", "direction": "YES", "resolved_correct": 1}
             for i in range(4)]
        b = a[:]
        self.assertIsNone(network.pairwise_stats(a, b))

    def test_perfect_agreement_high_correct_rate(self):
        a = [{"market_slug": f"m{i}", "direction": "YES", "resolved_correct": 1}
             for i in range(5)]
        b = [{"market_slug": f"m{i}", "direction": "YES", "resolved_correct": 1}
             for i in range(5)]
        stats = network.pairwise_stats(a, b)
        self.assertEqual(stats["agreement_rate"], 1.0)
        self.assertEqual(stats["both_correct_rate"], 1.0)
        self.assertEqual(network.classify_relationship(stats), "complementary")

    def test_high_agreement_low_accuracy_is_echo_chamber(self):
        a = [{"market_slug": f"m{i}", "direction": "YES", "resolved_correct": 0}
             for i in range(5)] + [
             {"market_slug": f"m{i}", "direction": "YES", "resolved_correct": 1}
             for i in range(5, 8)]
        b = [{"market_slug": f"m{i}", "direction": "YES", "resolved_correct": 0}
             for i in range(5)] + [
             {"market_slug": f"m{i}", "direction": "YES", "resolved_correct": 1}
             for i in range(5, 8)]
        stats = network.pairwise_stats(a, b)
        # agreement 1.0, both_correct 3/8 = 0.375
        self.assertGreater(stats["agreement_rate"], 0.85)
        self.assertLess(stats["both_correct_rate"], 0.65)
        self.assertEqual(network.classify_relationship(stats), "echo_chamber")

    def test_opposing_relationship(self):
        a = [{"market_slug": f"m{i}", "direction": "YES", "resolved_correct": 1}
             for i in range(5)]
        b = [{"market_slug": f"m{i}", "direction": "NO", "resolved_correct": 0}
             for i in range(5)]
        stats = network.pairwise_stats(a, b)
        self.assertEqual(stats["agreement_rate"], 0.0)
        self.assertEqual(network.classify_relationship(stats), "opposing")


class TestClustersAndConsensus(unittest.TestCase):
    def test_echo_chambers_union_connected(self):
        rels = [
            {"source_a": "a", "source_b": "b", "relationship_type": "echo_chamber"},
            {"source_a": "b", "source_b": "c", "relationship_type": "echo_chamber"},
            {"source_a": "d", "source_b": "e", "relationship_type": "echo_chamber"},
            {"source_a": "x", "source_b": "y", "relationship_type": "independent"},
        ]
        clusters = network.echo_chamber_clusters(rels)
        clusters_sets = [set(c) for c in clusters]
        self.assertIn({"a", "b", "c"}, clusters_sets)
        self.assertIn({"d", "e"}, clusters_sets)
        # Singletons must not appear.
        for c in clusters:
            self.assertGreater(len(c), 1)

    def test_consensus_downweights_cluster(self):
        preds = [
            {"source_handle": "a", "direction": "YES", "credibility": 0.9},
            {"source_handle": "b", "direction": "YES", "credibility": 0.9},
            {"source_handle": "c", "direction": "YES", "credibility": 0.9},
            {"source_handle": "solo", "direction": "NO", "credibility": 0.9},
        ]
        clusters = [["a", "b", "c"]]
        result = network.network_adjusted_consensus(preds, clusters)
        # a+b+c capped at ~1.0 total; solo contributes 0.9 → consensus ~ 1/1.9 = 0.526 YES
        self.assertLess(result["consensus_yes"], 0.7)
        self.assertEqual(result["effective_signal_count"], 2)


# ── Backtest engine ─────────────────────────────────────────────────────────


class TestKelly(unittest.TestCase):
    def test_no_edge_returns_zero(self):
        self.assertEqual(bt.kelly_fraction(0.5, 0.5), 0.0)
        self.assertEqual(bt.kelly_fraction(0.4, 0.5), 0.0)

    def test_capped_at_quarter(self):
        # Huge edge: we believe 0.95 at market price 0.20.
        f = bt.kelly_fraction(0.95, 0.20)
        self.assertLessEqual(f, 0.25)
        self.assertGreater(f, 0.0)

    def test_standard_edge(self):
        f = bt.kelly_fraction(0.6, 0.5)
        self.assertGreater(f, 0.0)
        self.assertLess(f, 0.25)


class TestSimulate(unittest.TestCase):
    def test_flat_stake_wins_and_losses(self):
        params = {
            "bet_sizing": "flat", "flat_bet_size": 100,
            "starting_bankroll": 1000,
            "min_ev": 0, "min_hours_remaining": 0,
            "categories": [], "source_handles": [],
            "date_from": 0, "date_to": 9999,
        }
        preds = [
            {"predicted_probability": 0.7, "yes_price": 0.5, "direction": "YES",
             "resolved_correct": 1, "content": "p1", "extracted_at": 10,
             "market_close_time": 100},
            {"predicted_probability": 0.6, "yes_price": 0.5, "direction": "YES",
             "resolved_correct": 0, "content": "p2", "extracted_at": 20,
             "market_close_time": 100},
        ]
        result = bt.simulate(params, preds)
        self.assertEqual(result["bet_count"], 2)
        self.assertEqual(result["wins"], 1)
        self.assertEqual(result["losses"], 1)
        # Win at 0.5 pays 1:1 → +100; loss → -100 → net 0.
        self.assertAlmostEqual(result["final_bankroll"], 1000.0, delta=1.0)

    def test_min_ev_filters(self):
        params = {
            "bet_sizing": "flat", "flat_bet_size": 100,
            "starting_bankroll": 1000,
            "min_ev": 0.20,  # require 20pp edge
            "min_hours_remaining": 0,
            "categories": [], "source_handles": [],
            "date_from": 0, "date_to": 9999,
        }
        preds = [
            {"predicted_probability": 0.55, "yes_price": 0.5,
             "direction": "YES", "resolved_correct": 1, "content": "tiny",
             "extracted_at": 10, "market_close_time": 100},
        ]
        result = bt.simulate(params, preds)
        self.assertEqual(result["bet_count"], 0)

    def test_max_drawdown(self):
        equity = [1000, 1200, 800, 900, 700]
        dd = bt.max_drawdown(equity)
        # Peak 1200 → trough 700 → drawdown 500/1200 = 0.4167
        self.assertAlmostEqual(dd, 500 / 1200, places=3)


# ── Insider scoring ─────────────────────────────────────────────────────────


class TestInsiderScore(unittest.TestCase):
    def test_strong_signal_caps_score(self):
        score = compute_insider_score(
            signal_strength="strong",
            disclosure_delay=1.0,
            amount_significance=1.0,
            correlation_confidence="high",
        )
        # 0.4*1 + 0.2*1 + 0.2*1 + 0.2*1 = 1.0
        self.assertAlmostEqual(score, 1.0, places=4)

    def test_weak_signal_low_score(self):
        score = compute_insider_score(
            signal_strength="weak",
            disclosure_delay=0.0,
            amount_significance=0.0,
            correlation_confidence="speculative",
        )
        # 0.4*0.3 + 0.2*0 + 0.2*0 + 0.2*0.15 = 0.15
        self.assertAlmostEqual(score, 0.15, places=4)

    def test_unknown_values_yield_zero(self):
        self.assertEqual(compute_insider_score(
            signal_strength=None, disclosure_delay=None,
            amount_significance=None, correlation_confidence=None,
        ), 0.0)


# ── Environmental conversions ───────────────────────────────────────────────


class TestEnvConversions(unittest.TestCase):
    def test_mt_passthrough(self):
        r = env.convert_co2(2.1, "co2_mt")
        self.assertEqual(r["value"], 2.1)

    def test_trees(self):
        r = env.convert_co2(1.0, "trees")
        self.assertEqual(r["value"], round(45_871, 4))

    def test_unknown_unit_falls_back(self):
        r = env.convert_co2(1.0, "made_up")
        self.assertEqual(r["unit_key"], "co2_mt")

    def test_negative_preserves_sign(self):
        r = env.convert_co2(-2.0, "trees")
        self.assertLess(r["value"], 0)

    def test_none_value(self):
        r = env.convert_co2(None, "cars")
        self.assertIsNone(r["value"])


if __name__ == "__main__":
    unittest.main()
