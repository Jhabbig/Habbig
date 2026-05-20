"""Tests for the election-night call state machine and chamber aggregation.

Run:
    cd backend && python3 test_election_night.py
"""
from __future__ import annotations

import sys

from election_night import (
    CALL_CALLED_D,
    CALL_CALLED_R,
    CALL_LEAN_D,
    CALL_LEAN_R,
    CALL_TOSSUP,
    aggregate_chamber,
    assemble_election_night,
    classify_call,
    polling_gap,
)


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_classify_called_d_requires_confidence():
    assert classify_call(forecast_d=0.95, confidence=0.8) == CALL_CALLED_D
    # Low confidence demotes to lean even at extreme probability
    assert classify_call(forecast_d=0.95, confidence=0.4) == CALL_LEAN_D
    print("PASS classify_called_d_requires_confidence")


def test_classify_called_r_requires_confidence():
    assert classify_call(forecast_d=0.05, confidence=0.8) == CALL_CALLED_R
    assert classify_call(forecast_d=0.05, confidence=0.4) == CALL_LEAN_R
    print("PASS classify_called_r_requires_confidence")


def test_smart_money_disagreement_demotes_call():
    """When smart money points the other way, a near-threshold call is
    downgraded to a lean. This is the conservative election-night posture."""
    out = classify_call(forecast_d=0.95, confidence=0.8, smart_money_direction="R")
    if out != CALL_LEAN_D:
        _fail(f"smart-money disagreement should demote: {out}")
    out = classify_call(forecast_d=0.05, confidence=0.8, smart_money_direction="D")
    if out != CALL_LEAN_R:
        _fail(f"smart-money disagreement should demote: {out}")
    print("PASS smart_money_disagreement_demotes_call")


def test_smart_money_agreement_keeps_call():
    out = classify_call(forecast_d=0.95, confidence=0.8, smart_money_direction="D")
    if out != CALL_CALLED_D:
        _fail(f"agreement should keep call: {out}")
    print("PASS smart_money_agreement_keeps_call")


def test_classify_tossup_when_forecast_missing():
    assert classify_call(forecast_d=None, confidence=0.9) == CALL_TOSSUP
    print("PASS classify_tossup_when_forecast_missing")


def test_classify_lean_thresholds():
    assert classify_call(forecast_d=0.70, confidence=0.5) == CALL_LEAN_D
    assert classify_call(forecast_d=0.30, confidence=0.5) == CALL_LEAN_R
    assert classify_call(forecast_d=0.50, confidence=0.5) == CALL_TOSSUP
    print("PASS classify_lean_thresholds")


def test_polling_gap_basic():
    assert polling_gap(forecast_d=0.55, polling_avg=0.50) == 5.0
    assert polling_gap(forecast_d=0.40, polling_avg=0.50) == -10.0
    print("PASS polling_gap_basic")


def test_polling_gap_handles_missing():
    assert polling_gap(forecast_d=None, polling_avg=0.50) is None
    assert polling_gap(forecast_d=0.50, polling_avg=None) is None
    print("PASS polling_gap_handles_missing")


def test_chamber_aggregation_floor_ceiling():
    races = [
        {"race_type": "senate", "call_state": CALL_CALLED_D},
        {"race_type": "senate", "call_state": CALL_CALLED_D},
        {"race_type": "senate", "call_state": CALL_LEAN_D},
        {"race_type": "senate", "call_state": CALL_TOSSUP},
        {"race_type": "senate", "call_state": CALL_LEAN_R},
        {"race_type": "senate", "call_state": CALL_CALLED_R},
        {"race_type": "house", "call_state": CALL_CALLED_D},  # different chamber, ignored
    ]
    s = aggregate_chamber(races, chamber="senate")
    if s["total"] != 6:
        _fail(f"senate total wrong: {s}")
    if s["called_d"] != 2 or s["lean_d"] != 1 or s["tossup"] != 1 or s["lean_r"] != 1 or s["called_r"] != 1:
        _fail(f"bucket counts wrong: {s}")
    # D floor = called_d; D ceiling = called_d + lean_d + tossup
    if s["d_floor"] != 2 or s["d_ceiling"] != 4:
        _fail(f"D floor/ceiling wrong: {s}")
    if s["r_floor"] != 1 or s["r_ceiling"] != 3:
        _fail(f"R floor/ceiling wrong: {s}")
    print("PASS chamber_aggregation_floor_ceiling")


def test_assemble_election_night_end_to_end():
    forecasts = [
        # Called D with smart-money agreement
        {"race_key": "senate_CA", "race_type": "senate", "state": "CA",
         "forecast_d": 0.92, "confidence": 0.8,
         "smart_money": {"available": True, "direction": "D"}},
        # Lean R (forecast strong R but smart money disagrees → demoted from called)
        {"race_key": "senate_TX", "race_type": "senate", "state": "TX",
         "forecast_d": 0.05, "confidence": 0.8,
         "smart_money": {"available": True, "direction": "D"}},
        # Tossup
        {"race_key": "senate_GA", "race_type": "senate", "state": "GA",
         "forecast_d": 0.50, "confidence": 0.7,
         "smart_money": {"available": False}},
    ]
    payload = assemble_election_night(
        forecasts=forecasts,
        polling_by_race={"senate_CA": 0.85, "senate_TX": 0.40},
    )
    if payload["counts"]["total_races"] != 3:
        _fail(f"total wrong: {payload['counts']}")
    if payload["counts"]["called"] != 1:
        _fail(f"called count wrong: {payload['counts']}")
    if payload["counts"]["tossups"] != 1:
        _fail(f"tossups count wrong: {payload['counts']}")
    # CA: 0.92 - 0.85 = 7pp gap (market is bullish on D vs polling)
    ca = next(r for r in payload["races"] if r["race_key"] == "senate_CA")
    if abs(ca["polling_gap_pp"] - 7.0) > 1e-6:
        _fail(f"CA polling gap wrong: {ca['polling_gap_pp']}")
    if ca["call_state"] != CALL_CALLED_D:
        _fail(f"CA should be called D: {ca['call_state']}")
    # TX: smart-money disagreement should demote called_r → lean_r
    tx = next(r for r in payload["races"] if r["race_key"] == "senate_TX")
    if tx["call_state"] != CALL_LEAN_R:
        _fail(f"TX should be lean R (demoted): {tx['call_state']}")
    # GA: tossup, no polling data
    ga = next(r for r in payload["races"] if r["race_key"] == "senate_GA")
    if ga["call_state"] != CALL_TOSSUP:
        _fail(f"GA should be tossup: {ga['call_state']}")
    if ga["polling_gap_pp"] is not None:
        _fail(f"GA polling gap should be None: {ga['polling_gap_pp']}")
    # Chamber summary
    sen = payload["chambers"]["senate"]
    if sen["total"] != 3 or sen["called_d"] != 1 or sen["lean_r"] != 1 or sen["tossup"] != 1:
        _fail(f"senate summary wrong: {sen}")
    print("PASS assemble_election_night_end_to_end")


def test_assemble_handles_empty_forecasts():
    payload = assemble_election_night(forecasts=[], polling_by_race={})
    if payload["counts"]["total_races"] != 0:
        _fail("empty input should yield zero counts")
    for chamber in ("senate", "house", "governor"):
        if payload["chambers"][chamber]["total"] != 0:
            _fail(f"{chamber} summary should be empty")
    print("PASS assemble_handles_empty_forecasts")


if __name__ == "__main__":
    test_classify_called_d_requires_confidence()
    test_classify_called_r_requires_confidence()
    test_smart_money_disagreement_demotes_call()
    test_smart_money_agreement_keeps_call()
    test_classify_tossup_when_forecast_missing()
    test_classify_lean_thresholds()
    test_polling_gap_basic()
    test_polling_gap_handles_missing()
    test_chamber_aggregation_floor_ceiling()
    test_assemble_election_night_end_to_end()
    test_assemble_handles_empty_forecasts()
    print("\nAll election-night tests passed.")
