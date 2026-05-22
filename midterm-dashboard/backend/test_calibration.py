"""Tests for the per-confidence-bucket calibration table.

Run:
    cd backend && python3 test_calibration.py
"""
from __future__ import annotations

import sys

from calibration import (
    BUCKET_EDGES,
    BUCKET_LABELS,
    _bucket_for,
    calibration_over_time,
    calibration_table,
)


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_bucket_boundaries():
    # Exact boundaries belong to the bucket below (lo inclusive, hi exclusive)
    # except 1.0 which sits in the top bucket.
    if _bucket_for(0.0) != 0:
        _fail("0.0 should land in bucket 0")
    if _bucket_for(0.5) != 2:
        _fail("0.5 should land in middle bucket")
    if _bucket_for(0.85) != 4:
        _fail("0.85 should land in top bucket")
    if _bucket_for(1.0) != 4:
        _fail("1.0 must not overflow — should clamp to top bucket")
    print("PASS bucket_boundaries")


def test_invalid_inputs_rejected():
    if _bucket_for(None) is not None:
        _fail("None should not place into a bucket")
    if _bucket_for(-0.1) is not None:
        _fail("Negative prob should not place into a bucket")
    if _bucket_for(1.5) is not None:
        _fail(">1 prob should not place into a bucket")
    if _bucket_for("nope") is not None:
        _fail("String should not place into a bucket")
    print("PASS invalid_inputs_rejected")


def test_perfectly_calibrated_buckets_have_zero_diff():
    """If every 80% call resolves 80% of the time, diff_pp should be 0."""
    samples = []
    # 10 races at p=0.9, 9 of them D-wins → 0.90 realized, diff=0
    for i in range(10):
        samples.append({"forecast_d": 0.9, "outcome_d": 1 if i < 9 else 0})
    # 10 races at p=0.5, 5 D-wins → 0.50 realized, diff=0
    for i in range(10):
        samples.append({"forecast_d": 0.5, "outcome_d": 1 if i < 5 else 0})
    t = calibration_table(samples)
    top = t["buckets"][4]
    mid = t["buckets"][2]
    if abs(top["diff_pp"]) > 0.01:
        _fail(f"top bucket should be perfectly calibrated: {top}")
    if abs(mid["diff_pp"]) > 0.01:
        _fail(f"mid bucket should be perfectly calibrated: {mid}")
    print("PASS perfectly_calibrated_buckets_have_zero_diff")


def test_brier_score_correct():
    """Brier score for a perfect coin-flip (0.5 → 0.5 realized) is 0.25."""
    samples = [
        {"forecast_d": 0.5, "outcome_d": 0},
        {"forecast_d": 0.5, "outcome_d": 1},
    ]
    t = calibration_table(samples)
    if abs(t["brier_score"] - 0.25) > 1e-6:
        _fail(f"expected 0.25, got {t['brier_score']}")
    print("PASS brier_score_correct")


def test_overconfident_call_shows_negative_diff():
    """Calling 95% but only 50% of those resolve D should show a large
    negative diff_pp in the top bucket — i.e. we were overconfident on D."""
    samples = [{"forecast_d": 0.95, "outcome_d": i % 2} for i in range(10)]
    t = calibration_table(samples)
    top = t["buckets"][4]
    if top["diff_pp"] >= 0:
        _fail(f"overconfident D should yield negative diff: {top}")
    print("PASS overconfident_call_shows_negative_diff")


def test_skips_unparseable_rows():
    samples = [
        {"forecast_d": 0.7, "outcome_d": 1},
        {"forecast_d": None, "outcome_d": 1},        # missing forecast
        {"forecast_d": 0.4, "outcome_d": None},      # missing outcome
        {"forecast_d": 0.5, "outcome_d": 2},         # invalid outcome
        {"forecast_d": "nope", "outcome_d": 0},     # unparseable
    ]
    t = calibration_table(samples)
    if t["n_total"] != 1:
        _fail(f"only the one clean row should count: n_total={t['n_total']}")
    print("PASS skips_unparseable_rows")


def test_empty_input_returns_nones():
    t = calibration_table([])
    if t["n_total"] != 0 or t["brier_score"] is not None:
        _fail(f"empty input should yield None brier: {t}")
    for b in t["buckets"]:
        if b["n"] != 0 or b["realized_d_rate"] is not None:
            _fail(f"empty bucket should be Noned: {b}")
    print("PASS empty_input_returns_nones")


def test_over_time_window_split():
    samples = []
    for i, ts in enumerate([
        "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
        "2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z",
    ]):
        samples.append({"forecast_d": 0.5, "outcome_d": i % 2, "snapshot_time": ts})
    ot = calibration_over_time(samples, n_windows=2)
    if ot["n_total"] != 4 or len(ot["windows"]) != 2:
        _fail(f"window split wrong: {ot}")
    if ot["windows"][0]["start"] >= ot["windows"][1]["start"]:
        _fail("windows must be chronological")
    print("PASS over_time_window_split")


def test_log_loss_reasonable():
    """Log loss for a perfectly-calibrated 0.5 forecast is ln(2) ≈ 0.693."""
    import math
    samples = [{"forecast_d": 0.5, "outcome_d": 1}, {"forecast_d": 0.5, "outcome_d": 0}]
    t = calibration_table(samples)
    if abs(t["log_loss"] - math.log(2)) > 1e-3:
        _fail(f"expected ~ln(2) for symmetric 0.5 forecast: {t['log_loss']}")
    print("PASS log_loss_reasonable")


if __name__ == "__main__":
    test_bucket_boundaries()
    test_invalid_inputs_rejected()
    test_perfectly_calibrated_buckets_have_zero_diff()
    test_brier_score_correct()
    test_overconfident_call_shows_negative_diff()
    test_skips_unparseable_rows()
    test_empty_input_returns_nones()
    test_over_time_window_split()
    test_log_loss_reasonable()
    print("\nAll calibration tests passed.")
