"""Brier score + reliability curve tests."""
from __future__ import annotations

from types import SimpleNamespace

from app.credibility.calibration import _outcome_value, compute_calibration


def _p(prob, outcome, correct):
    return SimpleNamespace(
        predicted_probability=prob,
        predicted_outcome=outcome,
        resolved_correct=correct,
    )


def test_outcome_value_yes_correct(): assert _outcome_value("Yes", True) == 1.0
def test_outcome_value_yes_wrong(): assert _outcome_value("Yes", False) == 0.0
def test_outcome_value_no_correct(): assert _outcome_value("No", True) == 0.0
def test_outcome_value_no_wrong(): assert _outcome_value("No", False) == 1.0
def test_outcome_value_unknown_returns_none(): assert _outcome_value("Maybe", True) is None


def test_calibration_perfect_forecaster():
    # Always 1.0 when YES happens, always 0.0 when NO happens.
    preds = [
        _p(1.0, "Yes", True),
        _p(0.0, "Yes", False),  # said 0% chance YES, NO happened -> y=0, prob=0 -> perfect
        _p(1.0, "Yes", True),
    ]
    stats = compute_calibration(preds)
    assert stats.brier_score == 0.0
    assert stats.n_scored == 3


def test_calibration_coin_flip_baseline():
    # All 0.5 predictions, half YES half NO. Brier = 0.25 (the noise baseline).
    preds = [
        _p(0.5, "Yes", True),   # y=1, brier contrib = (0.5-1)^2 = 0.25
        _p(0.5, "Yes", False),  # y=0, brier contrib = (0.5-0)^2 = 0.25
        _p(0.5, "Yes", True),
        _p(0.5, "Yes", False),
    ]
    stats = compute_calibration(preds)
    assert abs(stats.brier_score - 0.25) < 1e-9


def test_calibration_skips_predictions_without_probability():
    preds = [
        _p(None, "Yes", True),      # skipped
        _p(0.8, "Yes", True),       # y=1, brier = (0.8-1)^2 = 0.04
    ]
    stats = compute_calibration(preds)
    assert stats.n_scored == 1
    assert abs(stats.brier_score - 0.04) < 1e-9


def test_calibration_skips_unresolved():
    preds = [
        _p(0.8, "Yes", None),      # skipped
        _p(0.8, "Yes", True),
    ]
    stats = compute_calibration(preds)
    assert stats.n_scored == 1


def test_calibration_no_predictions_returns_none():
    stats = compute_calibration([])
    assert stats.brier_score is None
    assert stats.n_scored == 0
    assert stats.reliability_curve == []


def test_reliability_curve_buckets():
    # 4 predictions at 0.25, 0.55, 0.55, 0.85
    preds = [
        _p(0.25, "Yes", False),  # bin 2 (0.2-0.3), y=0
        _p(0.55, "Yes", True),   # bin 5 (0.5-0.6), y=1
        _p(0.55, "Yes", False),  # bin 5, y=0
        _p(0.85, "Yes", True),   # bin 8, y=1
    ]
    stats = compute_calibration(preds, n_bins=10)
    # Three populated bins
    by_bin = {round(mid, 2): (obs, n) for mid, obs, n in stats.reliability_curve}
    assert by_bin[0.25] == (0.0, 1)         # 0/1 in bin 2
    assert by_bin[0.55] == (0.5, 2)         # 1/2 in bin 5
    assert by_bin[0.85] == (1.0, 1)         # 1/1 in bin 8


def test_calibration_no_side_flips_outcome():
    # Source says "No" with 0.20 stated probability of YES. YES does NOT happen
    # -> resolved_correct=True -> y for YES = 0 -> brier = (0.20-0)^2 = 0.04
    preds = [_p(0.20, "No", True)]
    stats = compute_calibration(preds)
    assert abs(stats.brier_score - 0.04) < 1e-9
