"""Tests for edge scoring (F4) and false consensus detection (F5).

Tests the enrich_markets_with_intelligence function and the data-layer
dependencies it uses. HTTP endpoint tests are separate.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests import _testdb  # noqa: F401
import db  # noqa: E402
from backend.markets.unified_markets import (  # noqa: E402
    UnifiedMarket,
    enrich_markets_with_intelligence,
    _cache,
)


def _make_market(market_id: str, yes_price: float = 0.5, **kwargs) -> UnifiedMarket:
    """Create a minimal UnifiedMarket for testing."""
    return UnifiedMarket(
        id=market_id,
        source="polymarket",
        title=f"Test market {market_id}",
        category="politics",
        yes_price=yes_price,
        no_price=round(1 - yes_price, 4),
        volume_usd=100000,
        liquidity_usd=10000,
        close_time=None,
        status="active",
        outcome=None,
        url="",
        **kwargs,
    )


class TestEnrichMarketsWithIntelligence(unittest.TestCase):
    def setUp(self):
        # Clear the enrichment cache between tests
        _cache.pop("enriched_markets", None)

    def test_market_with_no_predictions_unchanged(self):
        market = _make_market("poly:no-preds-test")
        result = enrich_markets_with_intelligence([market])
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].betyc_ev_score)
        self.assertEqual(result[0].betyc_prediction_count, 0)

    def test_market_with_predictions_gets_edge(self):
        market_id = "poly:edge-test-001"
        # Create a source with credibility
        db.upsert_source_credibility("edge_src_1", 0.8, accuracy_unlocked=True,
                                      total_predictions=20, correct_predictions=16)
        # Create a YES prediction for this market
        db.create_prediction(
            source_handle="edge_src_1",
            content="I predict YES",
            category="politics",
            market_id=market_id,
            direction="YES",
            predicted_probability=0.75,
        )
        market = _make_market(market_id, yes_price=0.50)
        _cache.pop("enriched_markets", None)
        result = enrich_markets_with_intelligence([market])

        self.assertEqual(result[0].betyc_prediction_count, 1)
        self.assertIsNotNone(result[0].betyc_ev_score)
        # betyc says ~75% YES, market is 50% → edge should be positive
        self.assertGreater(result[0].betyc_ev_score, 0)

    def test_edge_is_negative_when_betyc_below_market(self):
        market_id = "poly:neg-edge-test"
        db.upsert_source_credibility("neg_edge_src", 0.7, accuracy_unlocked=True,
                                      total_predictions=15, correct_predictions=10)
        db.create_prediction(
            source_handle="neg_edge_src",
            content="I predict NO",
            category="politics",
            market_id=market_id,
            direction="NO",
            predicted_probability=0.30,  # 30% YES → 70% NO
        )
        market = _make_market(market_id, yes_price=0.80)
        _cache.pop("enriched_markets", None)
        result = enrich_markets_with_intelligence([market])

        # betyc ~30% YES, market 80% → negative edge
        self.assertLess(result[0].betyc_ev_score, 0)

    def test_consensus_yes_when_betyc_above_55(self):
        market_id = "poly:consensus-yes-test"
        db.upsert_source_credibility("cons_yes_src", 0.9, accuracy_unlocked=True,
                                      total_predictions=30, correct_predictions=27)
        db.create_prediction(
            source_handle="cons_yes_src",
            content="YES prediction",
            category="politics",
            market_id=market_id,
            direction="YES",
            predicted_probability=0.80,
        )
        market = _make_market(market_id, yes_price=0.50)
        _cache.pop("enriched_markets", None)
        result = enrich_markets_with_intelligence([market])

        self.assertEqual(result[0].betyc_consensus, "YES")


class TestFalseConsensusDetection(unittest.TestCase):
    def setUp(self):
        _cache.pop("enriched_markets", None)

    def test_extreme_market_with_disagreement_is_flagged(self):
        """Market at 85% YES but credibility says ~40% → false consensus."""
        market_id = "poly:false-consensus-test"
        db.upsert_source_credibility("fc_src", 0.8, accuracy_unlocked=True,
                                      total_predictions=20, correct_predictions=16)
        db.create_prediction(
            source_handle="fc_src",
            content="I predict NO here",
            category="politics",
            market_id=market_id,
            direction="NO",
            predicted_probability=0.35,
        )
        market = _make_market(market_id, yes_price=0.85)
        result = enrich_markets_with_intelligence([market])

        self.assertTrue(result[0].false_consensus)
        self.assertEqual(result[0].false_consensus_direction, "OVERPRICED")

    def test_non_extreme_market_not_flagged(self):
        """Market at 55% with some disagreement is NOT false consensus."""
        market_id = "poly:no-fc-test"
        db.upsert_source_credibility("nofc_src", 0.7, accuracy_unlocked=True,
                                      total_predictions=15, correct_predictions=10)
        db.create_prediction(
            source_handle="nofc_src",
            content="I predict NO",
            category="politics",
            market_id=market_id,
            direction="NO",
        )
        market = _make_market(market_id, yes_price=0.55)
        result = enrich_markets_with_intelligence([market])

        self.assertFalse(result[0].false_consensus)

    def test_extreme_market_with_agreement_not_flagged(self):
        """Market at 85% YES and credibility also says YES → NOT false consensus."""
        market_id = "poly:agree-fc-test"
        db.upsert_source_credibility("agree_src", 0.8, accuracy_unlocked=True,
                                      total_predictions=20, correct_predictions=16)
        db.create_prediction(
            source_handle="agree_src",
            content="YES for sure",
            category="politics",
            market_id=market_id,
            direction="YES",
            predicted_probability=0.90,
        )
        market = _make_market(market_id, yes_price=0.85)
        result = enrich_markets_with_intelligence([market])

        self.assertFalse(result[0].false_consensus)


class TestEndpointRegistration(unittest.TestCase):
    def test_top_edge_endpoint_exists(self):
        import server
        paths = {r.path for r in server.app.routes if hasattr(r, "path")}
        self.assertIn("/api/markets/top-edge", paths)

    def test_false_consensus_endpoint_exists(self):
        import server
        paths = {r.path for r in server.app.routes if hasattr(r, "path")}
        self.assertIn("/api/markets/false-consensus", paths)

    def test_top_edge_before_market_id_path(self):
        """top-edge must be registered BEFORE /{market_id:path} to avoid
        being consumed by the path converter."""
        import server
        route_paths = [r.path for r in server.app.routes if hasattr(r, "path")]
        if "/api/markets/top-edge" in route_paths and "/api/markets/unified/{market_id:path}" in route_paths:
            idx_edge = route_paths.index("/api/markets/top-edge")
            idx_path = route_paths.index("/api/markets/unified/{market_id:path}")
            self.assertLess(idx_edge, idx_path)


if __name__ == "__main__":
    unittest.main()
