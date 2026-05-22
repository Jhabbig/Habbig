"""Tests for the smart-money signal.

Run:
    cd backend && python3 test_smart_money.py
"""
from __future__ import annotations

import sys

from smart_money import (
    _classify_outcome_party,
    _flow_index_by_slug,
    race_smart_money,
)


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_outcome_party_yes_no_from_title():
    assert _classify_outcome_party("Yes", "Will Democrats win the Texas Senate?") == "democrat"
    assert _classify_outcome_party("No", "Will Democrats win the Texas Senate?") == "republican"
    assert _classify_outcome_party("Yes", "Will the GOP hold the Senate?") == "republican"
    print("PASS outcome_party_yes_no_from_title")


def test_outcome_party_named():
    assert _classify_outcome_party("Democratic", "any title") == "democrat"
    assert _classify_outcome_party("Republican", "any title") == "republican"
    assert _classify_outcome_party("GOP", "any title") == "republican"
    print("PASS outcome_party_named")


def test_flow_index_lowercases_slug():
    flows = [{"slug": "US-Senate-TX", "outcome": "Yes"}]
    idx = _flow_index_by_slug(flows)
    if "us-senate-tx" not in idx:
        _fail(f"index should normalise slug: {list(idx.keys())}")
    print("PASS flow_index_lowercases_slug")


def test_race_smart_money_aggregates_dem_lean():
    markets = [
        {
            "source": "polymarket",
            "slug": "us-senate-tx-2026",
            "title": "Will Democrats win the 2026 Texas Senate race?",
            "outcomes": [{"name": "Yes"}, {"name": "No"}],
        }
    ]
    flows = [
        {
            "slug": "us-senate-tx-2026",
            "outcome": "Yes",
            "total_position_usd": 80_000.0,
            "smart_wallet_count": 6,
            "avg_quality": 82,
            "wallets": [{"address": "0xA"}, {"address": "0xB"}],
        },
        {
            "slug": "us-senate-tx-2026",
            "outcome": "No",
            "total_position_usd": 20_000.0,
            "smart_wallet_count": 2,
            "avg_quality": 70,
            "wallets": [{"address": "0xC"}],
        },
    ]
    out = race_smart_money(
        race_key="senate_TX",
        race_polymarket_markets=markets,
        flows=flows,
    )
    if not out["available"]:
        _fail("available flag should be True when flows match")
    if out["direction"] != "D":
        _fail(f"expected D direction, got {out['direction']}")
    if abs(out["total_smart_usd"] - 100_000.0) > 1e-6:
        _fail(f"total mismatch: {out['total_smart_usd']}")
    if abs(out["lean_strength"] - 0.8) > 1e-6:
        _fail(f"lean strength should be 0.8, got {out['lean_strength']}")
    # 3 distinct wallets across both flows
    if out["smart_wallet_count"] != 3:
        _fail(f"expected 3 distinct wallets, got {out['smart_wallet_count']}")
    print("PASS race_smart_money_aggregates_dem_lean")


def test_race_smart_money_no_match():
    markets = [{"source": "polymarket", "slug": "us-senate-tx-2026", "title": "..."}]
    flows = [{"slug": "us-senate-fl-2026", "outcome": "Yes", "total_position_usd": 10_000.0, "smart_wallet_count": 3, "wallets": []}]
    out = race_smart_money(race_key="senate_TX", race_polymarket_markets=markets, flows=flows)
    if out["available"]:
        _fail("available flag should be False when no slug matches")
    if out["direction"] is not None:
        _fail(f"direction should be None: {out['direction']}")
    print("PASS race_smart_money_no_match")


def test_race_smart_money_empty_inputs():
    out = race_smart_money(race_key="senate_TX", race_polymarket_markets=[], flows=[])
    if out["available"] or out["total_smart_usd"] != 0:
        _fail(f"empty inputs should return inert response: {out}")
    print("PASS race_smart_money_empty_inputs")


def test_race_smart_money_handles_unclassifiable_outcomes():
    """Outcomes the party classifier can't read shouldn't crash the aggregator."""
    markets = [{"source": "polymarket", "slug": "x", "title": "Bulgarian elections 2026"}]
    flows = [{
        "slug": "x", "outcome": "GERB", "total_position_usd": 5_000.0,
        "smart_wallet_count": 2, "wallets": [{"address": "0xZ"}],
    }]
    out = race_smart_money(race_key="world_BG", race_polymarket_markets=markets, flows=flows)
    if out["direction"] is not None:
        _fail("unclassifiable outcomes shouldn't pick a party")
    # Should still report the flow exists
    if len(out["flows"]) != 1:
        _fail("unclassifiable flow should still surface in the detail list")
    print("PASS race_smart_money_handles_unclassifiable_outcomes")


if __name__ == "__main__":
    test_outcome_party_yes_no_from_title()
    test_outcome_party_named()
    test_flow_index_lowercases_slug()
    test_race_smart_money_aggregates_dem_lean()
    test_race_smart_money_no_match()
    test_race_smart_money_empty_inputs()
    test_race_smart_money_handles_unclassifiable_outcomes()
    print("\nAll smart-money tests passed.")
