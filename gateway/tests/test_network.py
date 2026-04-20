"""Tests for source network analysis — echo chamber detection + independence scoring.

Covers:
  - agreement_rate computed correctly from shared predictions
  - echo_chamber detected when agreement > 0.85 and accuracy < 0.65
  - complementary detected correctly
  - independent_signal_score = 0.0 for identical prediction twins
  - independent_signal_score high for sources who regularly disagree
  - network-adjusted consensus lower than naive for echo chamber groups
  - echo chamber cluster detection (connected components)
  - Sources with fewer than 5 shared markets return None relationship
"""

from __future__ import annotations

import time
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402
from intelligence.network import (  # noqa: E402
    compute_independent_signal_score,
    classify_relationship,
    compute_all_relationships,
    compute_network_adjusted_consensus,
)


def _seed_predictions(handle: str, market_id: str, direction: str,
                      resolved: bool = True, correct: bool = True) -> int:
    """Insert a single prediction row and return its id."""
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO predictions "
            "(source_handle, market_id, category, direction, content, "
            "extracted_at, resolved, resolved_correct, resolved_at) "
            "VALUES (?, ?, 'politics', ?, 'test prediction', ?, ?, ?, ?)",
            (handle, market_id, direction, now,
             1 if resolved else 0,
             (1 if correct else 0) if resolved else None,
             now if resolved else None),
        )
        return cur.lastrowid


class TestIndependentSignalScore(unittest.TestCase):
    """Unit tests for the scoring formula itself."""

    def test_perfect_echo_chamber_scores_low(self):
        """Two sources that always agree but are usually wrong → near 0."""
        score = compute_independent_signal_score(
            agreement_rate=0.95,
            both_correct_rate=0.3,
            markets_both_predicted=20,
        )
        self.assertLess(score, 0.2)

    def test_fully_independent_scores_high(self):
        """Two sources that rarely agree → high independence."""
        score = compute_independent_signal_score(
            agreement_rate=0.2,
            both_correct_rate=0.6,
            markets_both_predicted=20,
        )
        self.assertGreater(score, 0.7)

    def test_complementary_scores_medium_high(self):
        """High agreement + high accuracy → medium-high (not penalised as hard)."""
        score = compute_independent_signal_score(
            agreement_rate=0.85,
            both_correct_rate=0.90,
            markets_both_predicted=20,
        )
        # Should be higher than pure echo chamber (because accuracy_adjustment helps)
        echo_score = compute_independent_signal_score(0.85, 0.30, 20)
        self.assertGreater(score, echo_score)

    def test_small_sample_penalised(self):
        """Fewer than 10 shared markets → score scaled down by confidence."""
        full = compute_independent_signal_score(0.5, 0.5, 20)
        half = compute_independent_signal_score(0.5, 0.5, 5)
        self.assertAlmostEqual(half, full * 0.5, delta=0.05)

    def test_zero_shared_markets_is_zero(self):
        score = compute_independent_signal_score(0.9, 0.9, 0)
        self.assertEqual(score, 0.0)

    def test_identical_twins_score_near_zero(self):
        """Agreement 1.0, accuracy 0.5, plenty of data → very low."""
        score = compute_independent_signal_score(1.0, 0.5, 50)
        self.assertLess(score, 0.2)

    def test_score_clamped_to_unit_range(self):
        # Edge case: very low agreement + very high accuracy
        score = compute_independent_signal_score(0.0, 1.0, 100)
        self.assertLessEqual(score, 1.0)
        self.assertGreaterEqual(score, 0.0)


class TestClassifyRelationship(unittest.TestCase):
    def test_echo_chamber(self):
        self.assertEqual(classify_relationship(0.90, 0.55), "echo_chamber")

    def test_complementary(self):
        self.assertEqual(classify_relationship(0.80, 0.85), "complementary")

    def test_opposing(self):
        self.assertEqual(classify_relationship(0.20, 0.50), "opposing")

    def test_independent_default(self):
        self.assertEqual(classify_relationship(0.55, 0.60), "independent")

    def test_high_agreement_high_accuracy_is_complementary_not_echo(self):
        """The key distinction: echo chamber = agree AND wrong; complementary = agree AND right."""
        self.assertEqual(classify_relationship(0.88, 0.80), "complementary")
        self.assertEqual(classify_relationship(0.88, 0.55), "echo_chamber")


class TestComputeAllRelationships(unittest.TestCase):
    """Integration test using the full compute_all_relationships pipeline."""

    @classmethod
    def setUpClass(cls):
        """Seed 3 sources with overlapping predictions across 6 markets.

        source_echo_a and source_echo_b always agree (same direction) and
        are usually wrong → should be detected as echo chamber.
        source_independent disagrees with both → should be independent.
        """
        markets = [f"test-market-{i}" for i in range(6)]

        for m in markets:
            _seed_predictions("source_echo_a", m, "YES", resolved=True, correct=False)
            _seed_predictions("source_echo_b", m, "YES", resolved=True, correct=False)
            _seed_predictions("source_independent", m, "NO", resolved=True, correct=True)

        # Give source_echo_a and source_echo_b one correct prediction so they
        # aren't at exactly 0% accuracy (unrealistic)
        _seed_predictions("source_echo_a", "test-market-6", "YES", resolved=True, correct=True)
        _seed_predictions("source_echo_b", "test-market-6", "YES", resolved=True, correct=True)
        _seed_predictions("source_independent", "test-market-6", "NO", resolved=True, correct=False)

        # Ensure credibility rows exist
        db.upsert_source_credibility("source_echo_a", 0.45, total_predictions=7, correct_predictions=1)
        db.upsert_source_credibility("source_echo_b", 0.43, total_predictions=7, correct_predictions=1)
        db.upsert_source_credibility("source_independent", 0.78, total_predictions=7, correct_predictions=6)

        cls.result = compute_all_relationships()

    def test_relationships_computed(self):
        self.assertGreater(self.result["computed"], 0)

    def test_echo_pair_detected(self):
        rel = db.get_relationship_between("source_echo_a", "source_echo_b")
        self.assertIsNotNone(rel)
        self.assertEqual(rel["relationship_type"], "echo_chamber")
        self.assertGreater(rel["agreement_rate"], 0.85)

    def test_independent_pair_detected(self):
        rel = db.get_relationship_between("source_echo_a", "source_independent")
        self.assertIsNotNone(rel)
        # source_echo_a says YES, source_independent says NO → low agreement
        self.assertLess(rel["agreement_rate"], 0.3)
        self.assertIn(rel["relationship_type"], ("opposing", "independent"))

    def test_echo_cluster_found(self):
        network = db.get_latest_source_network()
        self.assertIsNotNone(network)
        clusters = network["echo_chamber_clusters"]
        # source_echo_a and source_echo_b should be in the same cluster
        found = any(
            "source_echo_a" in c and "source_echo_b" in c
            for c in clusters
        )
        self.assertTrue(found, f"echo cluster not found in {clusters}")

    def test_most_independent_includes_independent_source(self):
        network = db.get_latest_source_network()
        self.assertIsNotNone(network)
        top = network["most_independent_sources"]
        handles = [s["handle"] for s in top]
        self.assertIn("source_independent", handles)

    def test_fewer_than_5_shared_returns_none(self):
        """Sources with too few shared markets should have no relationship."""
        _seed_predictions("sparse_a", "sparse-market-1", "YES")
        _seed_predictions("sparse_b", "sparse-market-1", "NO")
        _seed_predictions("sparse_a", "sparse-market-2", "YES")
        _seed_predictions("sparse_b", "sparse-market-2", "YES")
        # Only 2 shared markets — below MIN_SHARED_MARKETS (5)
        rel = db.get_relationship_between("sparse_a", "sparse_b")
        self.assertIsNone(rel)


class TestNetworkAdjustedConsensus(unittest.TestCase):
    """The network-adjusted probability should be lower than naive when
    echo chamber sources are involved."""

    def test_echo_chamber_reduces_yes_probability(self):
        """3 YES predictions from an echo chamber should count less than 3 independent ones."""
        preds = [
            {"source_handle": "echo_a", "direction": "YES", "global_credibility": 0.7,
             "predicted_probability": None, "category_credibility": None, "accuracy_unlocked": 0},
            {"source_handle": "echo_b", "direction": "YES", "global_credibility": 0.7,
             "predicted_probability": None, "category_credibility": None, "accuracy_unlocked": 0},
            {"source_handle": "real_c", "direction": "NO", "global_credibility": 0.8,
             "predicted_probability": None, "category_credibility": None, "accuracy_unlocked": 1},
        ]
        # Without network: 2 YES vs 1 NO → naive leans YES
        naive = db.calculate_betyc_probability(preds)
        naive_yes = naive["betyc_yes_probability"]
        self.assertIsNotNone(naive_yes)
        self.assertGreater(naive_yes, 0.5)

        # With network: echo_a and echo_b are in an echo chamber → their
        # combined weight should drop, shifting the consensus toward NO
        relationships = {
            ("echo_a", "echo_b"): {
                "source_a": "echo_a", "source_b": "echo_b",
                "relationship_type": "echo_chamber",
                "independent_signal_score": 0.1,
                "agreement_rate": 0.92,
                "both_correct_rate": 0.40,
            },
        }
        adjusted = compute_network_adjusted_consensus(preds, relationships)
        self.assertTrue(adjusted["network_adjusted"])
        self.assertLess(adjusted["betyc_yes_probability"], naive_yes)
        self.assertEqual(adjusted["effective_signal_count"], 2)  # echo pair counts as ~1
        self.assertGreater(len(adjusted["echo_chambers_found"]), 0)

    def test_no_relationships_passes_through_naive(self):
        preds = [
            {"source_handle": "solo", "direction": "YES", "global_credibility": 0.6,
             "predicted_probability": None, "category_credibility": None, "accuracy_unlocked": 0},
        ]
        result = compute_network_adjusted_consensus(preds, None)
        self.assertFalse(result["network_adjusted"])

    def test_empty_predictions_returns_none(self):
        result = compute_network_adjusted_consensus([], None)
        self.assertIsNone(result["betyc_yes_probability"])
        self.assertEqual(result["effective_signal_count"], 0)


if __name__ == "__main__":
    unittest.main()
