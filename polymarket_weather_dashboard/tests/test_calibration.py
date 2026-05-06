"""Tests for the weather_calibration module.

These cover the modeling changes that actually move PnL: empirical sigma
floor from residuals, ensemble-quantile probabilities (distribution-free),
inverse-variance forecast blending, and the bootstrap Sharpe CI.
"""

import math

import pytest

import weather_calibration as wcal
from weather_pure import (
    empirical_cdf_above,
    empirical_cdf_below,
    empirical_quantile,
)


# ─── Empirical sigma floor (residual std) ─────────────────────────────────────

def _row(model, f, o):
    return {"model": model, "forecast_high": f, "observed_high": o}


def test_fit_residual_std_drops_models_below_min_n():
    rows = [_row("ecmwf", 70 + i, 70) for i in range(4)]  # only 4 — drop
    out = wcal.fit_residual_std(rows)
    assert "ecmwf" not in out


def test_fit_residual_std_computes_bias_and_std():
    # Forecasts are systematically 2°F too hot, with std ≈ sqrt(2)
    rows = [_row("gfs", 72, 70), _row("gfs", 71, 70), _row("gfs", 73, 70),
            _row("gfs", 70, 70), _row("gfs", 74, 70), _row("gfs", 71, 70)]
    out = wcal.fit_residual_std(rows)
    assert "gfs" in out
    assert out["gfs"]["bias"] == pytest.approx(1.833, abs=0.01)
    assert out["gfs"]["residual_std"] is not None
    assert out["gfs"]["n"] == 6


def test_consensus_sigma_floor_takes_median():
    residuals = {
        "a": {"residual_std": 2.0}, "b": {"residual_std": 3.0},
        "c": {"residual_std": 5.0},
    }
    assert wcal.consensus_sigma_floor(residuals) == 3.0


def test_consensus_sigma_floor_handles_missing():
    assert wcal.consensus_sigma_floor({}) is None
    assert wcal.consensus_sigma_floor({"a": {"residual_std": None}}) is None


def test_calibrated_sigma_takes_max():
    # Empirical floor 4°F should beat ensemble's 2°F
    assert wcal.calibrated_sigma(2.0, 4.0, 1.0) == 4.0
    # Inflate by lead multiplier
    assert wcal.calibrated_sigma(2.0, 4.0, 1.5) == pytest.approx(6.0, abs=0.01)
    # Both missing → None
    assert wcal.calibrated_sigma(None, None) is None
    # One missing → other wins
    assert wcal.calibrated_sigma(3.0, None, 1.0) == 3.0


# ─── Lead-time sigma fit ──────────────────────────────────────────────────────

def test_fit_leadtime_sigma_curve_default_when_too_few():
    out = wcal.fit_leadtime_sigma_curve([{"lead_days": 1, "residual": 0.5}])
    assert out["source"] == "default"
    assert out["k"] == 0.12


def test_fit_leadtime_sigma_curve_fits_when_enough_data():
    rows = []
    # Synthetic: residuals widen with lead — std ≈ 1 + 0.5*sqrt(d)
    import random
    rng = random.Random(42)
    for d in (1, 2, 3, 5, 7, 10):
        for _ in range(20):
            sigma = 1.0 + 0.5 * math.sqrt(d)
            rows.append({"lead_days": d, "residual": rng.gauss(0, sigma)})
    out = wcal.fit_leadtime_sigma_curve(rows)
    assert out["source"] == "fitted"
    assert out["n"] >= 100
    # k should be in roughly the right ballpark; we don't pin to exact value
    assert 0.05 <= out["k"] <= 1.5


def test_leadtime_multiplier_monotone():
    curve = {"k": 0.5, "intercept": 1.0}
    m_today = wcal.leadtime_multiplier(curve, 0)
    m_3d = wcal.leadtime_multiplier(curve, 3)
    m_10d = wcal.leadtime_multiplier(curve, 10)
    assert m_today == 1.0
    assert m_3d > m_today
    assert m_10d > m_3d
    # And the cap kicks in eventually
    assert wcal.leadtime_multiplier(curve, 1000, cap=3.0) == 3.0


# ─── Empirical CDF + ensemble probability ─────────────────────────────────────

def test_empirical_cdf_above_simple():
    members = [60, 65, 70, 75, 80]
    assert empirical_cdf_above(members, 70) == 3 / 5  # 70, 75, 80
    assert empirical_cdf_above(members, 0) == 1.0
    assert empirical_cdf_above(members, 100) == 0.0


def test_empirical_cdf_below_simple():
    members = [60, 65, 70, 75, 80]
    assert empirical_cdf_below(members, 70) == 3 / 5  # 60, 65, 70


def test_empirical_quantile_interpolation():
    members = [10, 20, 30, 40, 50]
    assert empirical_quantile(members, 0.5) == 30
    assert empirical_quantile(members, 0.0) == 10
    assert empirical_quantile(members, 1.0) == 50


def test_empirical_probability_above():
    members = list(range(60, 90))  # 30 members, 60..89
    info = {"threshold": 75, "is_over": True, "unit": "F"}
    p = wcal.empirical_probability(info, members)
    # 75..89 inclusive = 15/30 = 0.5
    assert p == pytest.approx(0.5, abs=0.01)


def test_empirical_probability_too_few_members_returns_none():
    info = {"threshold": 70, "is_over": True, "unit": "F"}
    assert wcal.empirical_probability(info, [70, 71, 72]) is None


def test_blended_probability_prefers_empirical_when_available():
    members = list(range(60, 90))
    info = {"threshold": 75, "is_over": True, "unit": "F"}
    out = wcal.blended_probability(info, mean=74.5, std=10, members=members)
    assert out["method"] == "empirical"
    assert out["empirical"] is not None
    assert out["gaussian"] is not None


def test_blended_probability_falls_back_to_gaussian():
    info = {"threshold": 75, "is_over": True, "unit": "F"}
    out = wcal.blended_probability(info, mean=74.5, std=2.0, members=None)
    assert out["method"] == "gaussian"
    assert out["empirical"] is None
    assert out["gaussian"] is not None


def test_blended_probability_handles_celsius():
    # 1.6°C threshold == 34.88°F. Members in °F.
    members = [30.0, 32.0, 34.0, 35.0, 36.0, 37.0, 38.0, 33.0, 31.0,
               34.5, 34.0, 35.5, 36.5, 37.5, 38.5]
    info = {"threshold": 1.6, "is_over": True, "unit": "C"}
    out = wcal.blended_probability(info, mean=35.0, std=2.0, members=members)
    assert out["method"] == "empirical"
    # Most members are above 34.88°F so probability should be > 0.5
    assert out["empirical"] > 0.5


# ─── Inverse-variance blend ───────────────────────────────────────────────────

def test_inverse_variance_blend_with_three_estimates():
    # Three Gaussians with different precision — sharper estimate should
    # dominate.
    estimates = [
        {"mean": 70.0, "std": 1.0, "weight_hint": 1.0},
        {"mean": 75.0, "std": 5.0, "weight_hint": 1.0},
        {"mean": 80.0, "std": 10.0, "weight_hint": 1.0},
    ]
    out = wcal.inverse_variance_blend(estimates)
    assert out is not None
    # std=1.0 has 1/sigma^2 = 1.00, std=5 has 0.04, std=10 has 0.01
    # so blended_mean ≈ (70*1 + 75*0.04 + 80*0.01) / 1.05 ≈ 70.286
    assert abs(out["mean"] - 70.286) < 0.05
    # Blended std should be sharper than any individual one
    assert out["std"] < 1.0


def test_inverse_variance_blend_skips_invalid():
    estimates = [
        {"mean": 70.0, "std": 0.0},  # invalid std
        {"mean": 75.0, "std": 5.0},
        {"mean": None, "std": 5.0},  # invalid mean
    ]
    out = wcal.inverse_variance_blend(estimates)
    assert out is not None
    assert out["mean"] == 75.0


def test_inverse_variance_blend_returns_none_when_empty():
    assert wcal.inverse_variance_blend([]) is None
    assert wcal.inverse_variance_blend([{"mean": None, "std": None}]) is None


def test_persistence_weight_decays_with_lead():
    assert wcal.persistence_weight_for_lead(1) == 1.0
    assert wcal.persistence_weight_for_lead(2) == pytest.approx(2 / 3, abs=0.01)
    assert wcal.persistence_weight_for_lead(4) == 0.0
    assert wcal.persistence_weight_for_lead(7) == 0.0
    assert wcal.persistence_weight_for_lead(0) == 0.0


# ─── Calibration metrics ──────────────────────────────────────────────────────

def test_brier_score_perfect_predictions():
    preds = [1.0, 0.0, 1.0, 0.0]
    outcomes = [1, 0, 1, 0]
    assert wcal.brier_score(preds, outcomes) == 0.0


def test_brier_score_constant_half_baseline():
    preds = [0.5] * 100
    outcomes = [1, 0] * 50
    assert wcal.brier_score(preds, outcomes) == pytest.approx(0.25, abs=0.001)


def test_log_loss_finite_on_overconfident_wrong():
    preds = [0.999]
    outcomes = [0]
    out = wcal.log_loss(preds, outcomes)
    assert out is not None
    assert math.isfinite(out)
    assert out > 5  # large loss but finite


def test_reliability_diagram_buckets():
    # 50 predictions, half at 0.2 (true 0% rate) and half at 0.8 (true 100% rate)
    preds = [0.2] * 50 + [0.8] * 50
    outcomes = [0] * 50 + [1] * 50
    diag = wcal.reliability_diagram(preds, outcomes, n_bins=10)
    # We should get exactly two non-empty buckets
    non_empty = [b for b in diag if b["n"] > 0]
    assert len(non_empty) == 2
    # The 0.2 bucket should be perfectly calibrated (predicted 0.2, actual 0)
    bucket_low = [b for b in non_empty if b["bin_lo"] <= 0.2 < b["bin_hi"]][0]
    assert bucket_low["actual_rate"] == 0.0
    # The 0.8 bucket should be perfectly calibrated (predicted 0.8, actual 1)
    bucket_hi = [b for b in non_empty if b["bin_lo"] <= 0.8 < b["bin_hi"]][0]
    assert bucket_hi["actual_rate"] == 1.0


# ─── Bootstrap Sharpe ─────────────────────────────────────────────────────────

def test_bootstrap_sharpe_returns_ci_band():
    # 100 random PnLs around 0 — Sharpe should be near 0 and CI should
    # straddle it.
    import random
    rng = random.Random(0)
    pnls = [rng.gauss(0.05, 0.5) for _ in range(100)]
    out = wcal.bootstrap_sharpe(pnls, n_resamples=500, seed=1)
    assert out["n"] == 100
    assert out["lo"] < out["point"] < out["hi"]
    # Width should be non-trivial — that's the whole point of the CI
    assert out["hi"] - out["lo"] > 0.05


def test_bootstrap_sharpe_handles_too_few():
    out = wcal.bootstrap_sharpe([0.1, 0.2], n_resamples=100)
    assert out["point"] is None
    assert out["lo"] is None
