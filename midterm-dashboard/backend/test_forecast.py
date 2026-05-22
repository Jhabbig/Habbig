"""Tests for the narve.ai house forecast ensemble.

Run:
    cd backend && python3 test_forecast.py
"""
from __future__ import annotations

import sys

from forecast import (
    DEFAULT_WEIGHTS,
    MIN_RESOLVED_FOR_BRIER,
    derive_weights,
    forecast_for_race,
    forecast_many,
)


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_cold_start_uses_defaults():
    f = forecast_for_race(
        race_key="senate_TX",
        source_probs={"polymarket": 0.42, "kalshi": 0.44, "manifold": 0.40},
    )
    assert f["method"] == "default_weights", f["method"]
    assert f["n_sources"] == 3
    assert 0.40 <= f["forecast_d"] <= 0.44, f
    print("PASS cold_start_uses_defaults")


def test_brier_weights_when_coverage_is_sufficient():
    brier = {"polymarket": 0.08, "kalshi": 0.10, "manifold": 0.20}
    coverage = {
        "polymarket": {"resolved_races": MIN_RESOLVED_FOR_BRIER + 1},
        "kalshi": {"resolved_races": MIN_RESOLVED_FOR_BRIER + 1},
        "manifold": {"resolved_races": MIN_RESOLVED_FOR_BRIER + 1},
    }
    f = forecast_for_race(
        race_key="senate_TX",
        source_probs={"polymarket": 0.42, "kalshi": 0.44, "manifold": 0.30},
        brier=brier,
        coverage=coverage,
    )
    assert f["method"] == "brier_weighted", f["method"]
    # Polymarket has the lowest Brier so it should weight highest
    if not (f["weights"]["polymarket"] > f["weights"]["manifold"]):
        _fail(f"weights ordering: {f['weights']}")
    # Forecast pulls toward the higher-weight cluster (around 0.42-0.44)
    if not (f["forecast_d"] > 0.40):
        _fail(f"forecast not pulled by Brier weighting: {f}")
    print("PASS brier_weights_when_coverage_is_sufficient")


def test_brier_falls_back_when_coverage_thin():
    """A source with low resolved-race count keeps its default weight."""
    brier = {"polymarket": 0.04, "kalshi": 0.04}
    coverage = {
        "polymarket": {"resolved_races": MIN_RESOLVED_FOR_BRIER - 1},
        "kalshi": {"resolved_races": MIN_RESOLVED_FOR_BRIER - 1},
    }
    weights = derive_weights(brier, coverage)
    # Should fall back to defaults because coverage is below threshold
    if abs(weights["polymarket"] - DEFAULT_WEIGHTS["polymarket"]) > 1e-9:
        _fail(f"expected default weight, got {weights}")
    print("PASS brier_falls_back_when_coverage_thin")


def test_no_sources_returns_null():
    f = forecast_for_race(race_key="senate_XX", source_probs={})
    if f["forecast_d"] is not None or f["n_sources"] != 0:
        _fail(f"empty source set should return forecast_d=None: {f}")
    print("PASS no_sources_returns_null")


def test_invalid_probabilities_are_dropped():
    f = forecast_for_race(
        race_key="senate_TX",
        source_probs={
            "polymarket": 0.55,  # valid
            "kalshi": "not a number",  # invalid string
            "manifold": -0.1,  # out of range
            "metaculus": 1.5,  # out of range
        },
    )
    if f["sources_used"] != ["polymarket"]:
        _fail(f"invalid probs should be dropped: {f['sources_used']}")
    print("PASS invalid_probabilities_are_dropped")


def test_confidence_drops_with_high_spread():
    """Two sources at 0.3 and 0.8 (spread=0.5) should give very low agreement."""
    f_tight = forecast_for_race(
        race_key="senate_TX",
        source_probs={"polymarket": 0.50, "kalshi": 0.51, "manifold": 0.49, "metaculus": 0.50, "polling": 0.50, "predictit": 0.50},
    )
    f_wide = forecast_for_race(
        race_key="senate_TX",
        source_probs={"polymarket": 0.30, "kalshi": 0.80, "manifold": 0.30, "metaculus": 0.80, "polling": 0.30, "predictit": 0.80},
    )
    if f_wide["confidence"] >= f_tight["confidence"]:
        _fail(
            f"wide-spread confidence {f_wide['confidence']} should be lower than "
            f"tight-spread {f_tight['confidence']}"
        )
    print("PASS confidence_drops_with_high_spread")


def test_forecast_many_handles_string_details():
    snaps = [
        {
            "race_key": "senate_TX",
            "race_type": "senate",
            "state": "TX",
            "polymarket_prob": 0.42,
            "kalshi_prob": 0.44,
            "predictit_prob": None,
            "polling_avg": None,
            "divergence_details": '{"manifold": 0.40, "metaculus": 0.41}',
            "snapshot_time": "2026-05-06T00:00:00Z",
        },
    ]
    fs = forecast_many(snaps)
    if len(fs) != 1 or fs[0]["n_sources"] != 4:
        _fail(f"expected 4 sources from JSON-string details: {fs}")
    print("PASS forecast_many_handles_string_details")


def test_forecast_normalizes_within_bounds():
    """Forecast probability must always be in [0, 1] regardless of weights."""
    f = forecast_for_race(
        race_key="senate_TX",
        source_probs={"polymarket": 0.99, "kalshi": 0.01},
    )
    if not (0.0 <= f["forecast_d"] <= 1.0):
        _fail(f"forecast out of bounds: {f}")
    print("PASS forecast_normalizes_within_bounds")


if __name__ == "__main__":
    test_cold_start_uses_defaults()
    test_brier_weights_when_coverage_is_sufficient()
    test_brier_falls_back_when_coverage_thin()
    test_no_sources_returns_null()
    test_invalid_probabilities_are_dropped()
    test_confidence_drops_with_high_spread()
    test_forecast_many_handles_string_details()
    test_forecast_normalizes_within_bounds()
    print("\nAll forecast tests passed.")
