"""Tests for the common-factor conditional forecast model.

Run:
    cd backend && python3 test_conditional.py
"""
from __future__ import annotations

import sys

from conditional import (
    MAX_DELTA,
    compute_conditional,
    correlation,
    joint_distribution_summary,
)


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _race(rk, rt, st, p):
    return {"race_key": rk, "race_type": rt, "state": st, "forecast_d": p, "confidence": 0.7}


def test_conditioned_race_pinned_to_outcome():
    forecasts = [_race("senate_PA", "senate", "PA", 0.55)]
    r = compute_conditional(forecasts=forecasts, conditioned_race_key="senate_PA", conditioned_outcome="D")
    pa = r["races"][0]
    if pa["forecast_d"] != 1.0:
        _fail(f"D condition should pin to 1.0: {pa}")
    if not pa.get("conditioned"):
        _fail("conditioned flag should be set on the pinned race")

    r2 = compute_conditional(forecasts=forecasts, conditioned_race_key="senate_PA", conditioned_outcome="R")
    if r2["races"][0]["forecast_d"] != 0.0:
        _fail(f"R condition should pin to 0.0: {r2['races'][0]}")
    print("PASS conditioned_race_pinned_to_outcome")


def test_d_outcome_pulls_others_d():
    forecasts = [
        _race("senate_PA", "senate", "PA", 0.50),
        _race("senate_MI", "senate", "MI", 0.50),
    ]
    r = compute_conditional(forecasts=forecasts, conditioned_race_key="senate_PA", conditioned_outcome="D")
    mi = next(x for x in r["races"] if x["race_key"] == "senate_MI")
    if mi["delta_pp"] <= 0:
        _fail(f"D outcome should shift MI toward D: {mi}")
    if mi["forecast_d"] <= 0.50:
        _fail(f"MI forecast_d should rise: {mi}")
    print("PASS d_outcome_pulls_others_d")


def test_r_outcome_pulls_others_r():
    forecasts = [
        _race("senate_PA", "senate", "PA", 0.50),
        _race("senate_MI", "senate", "MI", 0.50),
    ]
    r = compute_conditional(forecasts=forecasts, conditioned_race_key="senate_PA", conditioned_outcome="R")
    mi = next(x for x in r["races"] if x["race_key"] == "senate_MI")
    if mi["delta_pp"] >= 0:
        _fail(f"R outcome should shift MI toward R: {mi}")
    print("PASS r_outcome_pulls_others_r")


def test_competitive_races_move_more_than_safe_races():
    """A 50% race should shift more than a 90% race under the same swing."""
    forecasts = [
        _race("senate_PA", "senate", "PA", 0.50),
        _race("senate_MI", "senate", "MI", 0.50),  # competitive
        _race("senate_CA", "senate", "CA", 0.90),  # safe D
    ]
    r = compute_conditional(forecasts=forecasts, conditioned_race_key="senate_PA", conditioned_outcome="D")
    mi = next(x for x in r["races"] if x["race_key"] == "senate_MI")
    ca = next(x for x in r["races"] if x["race_key"] == "senate_CA")
    if abs(mi["delta_pp"]) <= abs(ca["delta_pp"]):
        _fail(f"competitive race should move more: mi={mi['delta_pp']} ca={ca['delta_pp']}")
    print("PASS competitive_races_move_more_than_safe_races")


def test_same_region_correlation_higher_than_cross_region():
    """PA-NY (both Northeast) should correlate higher than PA-TX (NE/South)."""
    pa = {"state": "PA", "race_type": "senate"}
    ny = {"state": "NY", "race_type": "senate"}
    tx = {"state": "TX", "race_type": "senate"}
    if correlation(pa, ny) <= correlation(pa, tx):
        _fail(f"NE-NE should correlate higher than NE-S: {correlation(pa, ny)} vs {correlation(pa, tx)}")
    print("PASS same_region_correlation_higher_than_cross_region")


def test_chamber_correlation_senate_higher_than_house():
    """Two statewide races correlate higher than a statewide + house race."""
    pa_sen = {"state": "PA", "race_type": "senate"}
    pa_gov = {"state": "PA", "race_type": "governor"}
    pa_house = {"state": "PA", "race_type": "house"}
    if correlation(pa_sen, pa_gov) <= correlation(pa_sen, pa_house):
        _fail("two statewide races should correlate higher than statewide + house")
    print("PASS chamber_correlation_senate_higher_than_house")


def test_delta_is_capped():
    """No single race should be allowed to push another race by more than MAX_DELTA."""
    forecasts = [
        _race("senate_PA", "senate", "PA", 0.50),
        _race("senate_NY", "senate", "NY", 0.50),  # same chamber + region
    ]
    r = compute_conditional(forecasts=forecasts, conditioned_race_key="senate_PA", conditioned_outcome="D")
    ny = next(x for x in r["races"] if x["race_key"] == "senate_NY")
    if abs(ny["delta_pp"]) > MAX_DELTA * 100 + 0.01:
        _fail(f"delta capped at {MAX_DELTA * 100}pp; got {ny['delta_pp']}")
    print("PASS delta_is_capped")


def test_missing_race_returns_unavailable():
    forecasts = [_race("senate_PA", "senate", "PA", 0.5)]
    r = compute_conditional(forecasts=forecasts, conditioned_race_key="senate_ZZ", conditioned_outcome="D")
    if r["available"]:
        _fail("missing race should mark response as unavailable")
    print("PASS missing_race_returns_unavailable")


def test_invalid_outcome_raises():
    try:
        compute_conditional(forecasts=[], conditioned_race_key="x", conditioned_outcome="X")
    except ValueError:
        print("PASS invalid_outcome_raises")
        return
    _fail("invalid outcome should raise ValueError")


def test_joint_distribution_summary_reasonable():
    """The MC expected D should be close to summing the unconditional p's
    for very stable forecasts, and the chamber_total should match input."""
    forecasts = [
        _race("senate_AA", "senate", "PA", 0.30),
        _race("senate_BB", "senate", "MI", 0.50),
        _race("senate_CC", "senate", "CA", 0.80),
        _race("house_DD",  "house",  "TX", 0.40),  # different chamber, should be ignored
    ]
    js = joint_distribution_summary(forecasts, chamber="senate")
    if js["chamber_total"] != 3:
        _fail(f"senate count wrong: {js}")
    naive_sum = 0.30 + 0.50 + 0.80
    if abs(js["expected_d"] - naive_sum) > 0.5:
        _fail(f"expected D far from naive sum: {js['expected_d']} vs ~{naive_sum}")
    print("PASS joint_distribution_summary_reasonable")


def test_passthrough_fields_preserved():
    """Conditional output should keep race fields the frontend needs."""
    forecasts = [
        {"race_key": "senate_PA", "race_type": "senate", "state": "PA",
         "forecast_d": 0.5, "confidence": 0.7, "n_sources": 4,
         "smart_money": {"available": True, "direction": "D"}},
        {"race_key": "senate_MI", "race_type": "senate", "state": "MI",
         "forecast_d": 0.55, "confidence": 0.6, "n_sources": 3,
         "smart_money": {"available": False}},
    ]
    r = compute_conditional(forecasts=forecasts, conditioned_race_key="senate_PA", conditioned_outcome="D")
    mi = next(x for x in r["races"] if x["race_key"] == "senate_MI")
    if mi.get("n_sources") != 3:
        _fail("n_sources should be preserved")
    if mi.get("smart_money", {}).get("available") is not False:
        _fail("smart_money should be preserved")
    print("PASS passthrough_fields_preserved")


if __name__ == "__main__":
    test_conditioned_race_pinned_to_outcome()
    test_d_outcome_pulls_others_d()
    test_r_outcome_pulls_others_r()
    test_competitive_races_move_more_than_safe_races()
    test_same_region_correlation_higher_than_cross_region()
    test_chamber_correlation_senate_higher_than_house()
    test_delta_is_capped()
    test_missing_race_returns_unavailable()
    test_invalid_outcome_raises()
    test_joint_distribution_summary_reasonable()
    test_passthrough_fields_preserved()
    print("\nAll conditional tests passed.")
