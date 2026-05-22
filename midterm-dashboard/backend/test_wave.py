"""Tests for the wave-election scenario model.

Run:
    cd backend && python3 test_wave.py
"""
from __future__ import annotations

import sys

from conditional import MAX_DELTA, apply_wave_swing


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _race(rk, rt, st, p):
    return {"race_key": rk, "race_type": rt, "state": st, "forecast_d": p}


def test_wave_d_swing_pulls_all_races_toward_d():
    forecasts = [
        _race("senate_PA", "senate", "PA", 0.50),
        _race("senate_MI", "senate", "MI", 0.40),
        _race("house_CA",  "house",  "CA", 0.55),
    ]
    out = apply_wave_swing(forecasts, swing_pp=5)
    for r in out["races"]:
        if r["delta_pp"] < 0:
            _fail(f"D+5 swing should not move any race toward R: {r}")
    print("PASS wave_d_swing_pulls_all_races_toward_d")


def test_wave_r_swing_inverts():
    forecasts = [_race("senate_PA", "senate", "PA", 0.50)]
    d = apply_wave_swing(forecasts, swing_pp=5)
    r = apply_wave_swing(forecasts, swing_pp=-5)
    d_delta = d["races"][0]["delta_pp"]
    r_delta = r["races"][0]["delta_pp"]
    if d_delta <= 0 or r_delta >= 0:
        _fail(f"signs wrong: d={d_delta} r={r_delta}")
    if abs(d_delta + r_delta) > 0.01:
        _fail(f"D+5 and R+5 should be approximately symmetric: {d_delta} vs {r_delta}")
    print("PASS wave_r_swing_inverts")


def test_wave_zero_swing_is_noop():
    forecasts = [_race("senate_PA", "senate", "PA", 0.5)]
    out = apply_wave_swing(forecasts, swing_pp=0)
    if abs(out["races"][0]["delta_pp"]) > 0.01:
        _fail(f"zero swing should not move anything: {out}")
    print("PASS wave_zero_swing_is_noop")


def test_wave_competitive_races_move_more():
    forecasts = [
        _race("senate_CA", "senate", "CA", 0.95),  # safe D
        _race("senate_PA", "senate", "PA", 0.50),  # tossup
    ]
    out = apply_wave_swing(forecasts, swing_pp=5)
    pa = next(r for r in out["races"] if r["race_key"] == "senate_PA")
    ca = next(r for r in out["races"] if r["race_key"] == "senate_CA")
    if abs(pa["delta_pp"]) <= abs(ca["delta_pp"]):
        _fail(f"competitive should move more: pa={pa['delta_pp']} ca={ca['delta_pp']}")
    print("PASS wave_competitive_races_move_more")


def test_wave_delta_capped():
    forecasts = [_race("senate_PA", "senate", "PA", 0.5)]
    # An absurdly large swing should still be capped per-race
    out = apply_wave_swing(forecasts, swing_pp=100)
    if abs(out["races"][0]["delta_pp"]) > MAX_DELTA * 100 + 0.01:
        _fail(f"delta capped at {MAX_DELTA * 100}pp; got {out['races'][0]['delta_pp']}")
    print("PASS wave_delta_capped")


def test_wave_chamber_summary():
    """Chamber bucket counts should reflect D/R wins under the swing."""
    forecasts = [
        _race("senate_AA", "senate", "PA", 0.30),
        _race("senate_BB", "senate", "MI", 0.50),
        _race("senate_CC", "senate", "CA", 0.80),
        _race("house_DD",  "house",  "TX", 0.55),
    ]
    out = apply_wave_swing(forecasts, swing_pp=5)
    sen = out["chambers"]["senate"]
    if sen["total"] != 3:
        _fail(f"senate total wrong: {sen}")
    if sen["d"] + sen["r"] != sen["total"]:
        _fail(f"d+r should equal total: {sen}")
    # House should be its own bucket
    house = out["chambers"]["house"]
    if house["total"] != 1:
        _fail(f"house total wrong: {house}")
    print("PASS wave_chamber_summary")


def test_wave_handles_missing_forecast():
    forecasts = [
        _race("senate_PA", "senate", "PA", None),
        _race("senate_MI", "senate", "MI", 0.5),
    ]
    out = apply_wave_swing(forecasts, swing_pp=5)
    pa = next(r for r in out["races"] if r["race_key"] == "senate_PA")
    if pa["delta_pp"] != 0.0:
        _fail("missing forecast should yield delta=0")
    if pa["forecast_d"] is not None:
        _fail("missing forecast should pass through as None")
    print("PASS wave_handles_missing_forecast")


def test_wave_non_numeric_swing_is_zero():
    forecasts = [_race("senate_PA", "senate", "PA", 0.5)]
    out = apply_wave_swing(forecasts, swing_pp="not a number")
    if abs(out["races"][0]["delta_pp"]) > 0.01:
        _fail(f"non-numeric swing should be treated as 0: {out}")
    print("PASS wave_non_numeric_swing_is_zero")


if __name__ == "__main__":
    test_wave_d_swing_pulls_all_races_toward_d()
    test_wave_r_swing_inverts()
    test_wave_zero_swing_is_noop()
    test_wave_competitive_races_move_more()
    test_wave_delta_capped()
    test_wave_chamber_summary()
    test_wave_handles_missing_forecast()
    test_wave_non_numeric_swing_is_zero()
    print("\nAll wave tests passed.")
