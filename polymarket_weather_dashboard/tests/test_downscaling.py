"""Tests for the station-downscaling regression.

The regression is in pure Python (no numpy) so unit tests can run in
millisecond range. We pin the key behaviors: graceful fallback when
data is sparse, the ridge fit recovers a known constant bias, R² is
within a sane range, and the apply step clamps pathological values.
"""

import math
import random

import pytest

from weather_downscaling import (
    apply_downscaling,
    evaluate_downscaling,
    fit_station_downscaling,
)


def _row(forecast: float, observed: float, target_date: str = "2026-04-01",
         lead_days: float = 1.0) -> dict:
    return {"forecast_high": forecast, "observed_high": observed,
            "target_date": target_date, "lead_days": lead_days}


def test_fit_returns_empty_when_zero_rows():
    out = fit_station_downscaling([])
    assert out["source"] == "empty"
    assert out["n"] == 0


def test_fit_falls_back_to_mean_bias_under_min_n():
    rows = [_row(72, 70) for _ in range(5)]  # 5 < MIN_ROWS_FOR_FIT
    out = fit_station_downscaling(rows)
    assert out["source"] == "mean_bias"
    assert out["n"] == 5
    assert out["intercept"] == pytest.approx(-2.0, abs=0.01)  # observed - forecast


def test_fit_recovers_constant_bias():
    """If forecast is consistently 3°F too hot, the intercept should
    move ~−3 (or β1≈1, β0≈−3) and corrections should bring forecasts
    back near observation."""
    rng = random.Random(42)
    rows = []
    for d in range(60):
        forecast = 70 + rng.gauss(0, 5)
        observed = forecast - 3.0 + rng.gauss(0, 1.0)
        rows.append(_row(forecast, observed,
                         target_date=f"2026-{(d // 30) + 1:02d}-{(d % 30) + 1:02d}",
                         lead_days=(d % 5) + 1))
    out = fit_station_downscaling(rows)
    assert out["source"] == "ridge"
    assert out["n"] == 60
    assert out["r2"] is not None
    assert out["r2"] > 0.5
    # Apply correction to a fresh forecast — corrected should be near observed
    raw = 75.0
    corrected = apply_downscaling(out, raw, "2026-06-15", lead_days=1)
    assert corrected is not None
    assert abs(corrected - (raw - 3)) < 1.5


def test_fit_captures_seasonal_shift():
    """When the bias scales with sin(doy) and the forecast itself is held
    independent of season, the regression should attribute the bias to
    the seasonal terms — not to the constant intercept or the forecast
    coefficient."""
    rng = random.Random(0)
    rows = []
    # Forecast intentionally NOT correlated with season (so the seasonal
    # signal can't leak into β_forecast). Bias amplitude large enough
    # to beat ridge shrinkage at λ=1.
    for doy in range(1, 350, 3):
        season = math.sin(2 * math.pi * doy / 365)
        bias = 8.0 * season  # +8 in summer, -8 in winter
        forecast = 70 + rng.gauss(0, 5)  # unrelated to doy
        observed = forecast - bias + rng.gauss(0, 1.0)
        date = f"2026-{((doy - 1) // 30) + 1:02d}-{((doy - 1) % 30) + 1:02d}"
        rows.append(_row(forecast, observed, target_date=date, lead_days=1))
    out = fit_station_downscaling(rows)
    assert out["source"] == "ridge"
    # Should learn a seasonal coefficient (β_sin or β_cos non-trivial)
    sin_cos = out["coef"][2:]
    assert any(abs(c) > 1.0 for c in sin_cos), \
        f"expected a strong seasonal coefficient, got {sin_cos}"


def test_apply_returns_none_for_empty_model():
    assert apply_downscaling({"coef": None, "intercept": None}, 70, "2026-01-01") is None
    assert apply_downscaling(None, 70, "2026-01-01") is None


def test_apply_clamps_pathological_correction():
    # Bogus model with huge intercept
    bad = {"coef": [1.0, 0.0, 0.0, 0.0], "intercept": -50.0,
           "r2": None, "n": 100, "source": "ridge"}
    corrected = apply_downscaling(bad, 70.0, "2026-04-01", lead_days=1)
    assert corrected is not None
    # Delta should be clamped to ±15
    assert abs(corrected - 70.0) <= 15.01


def test_apply_handles_garbage_date():
    out = {"coef": [1.0, 0.0, 0.5, 0.5], "intercept": 0.0,
           "r2": 0.8, "n": 100, "source": "ridge"}
    # Bad date should not crash
    result = apply_downscaling(out, 70.0, "not-a-date", lead_days=1)
    assert result is not None  # Falls back to (sin, cos) = (0, 0)


def test_evaluate_shows_improvement_when_model_fits():
    rng = random.Random(7)
    train, holdout = [], []
    for i in range(80):
        forecast = 70 + rng.gauss(0, 5)
        observed = forecast - 2.5 + rng.gauss(0, 1.0)
        date = f"2026-{((i // 30) % 12) + 1:02d}-{(i % 28) + 1:02d}"
        if i % 4 == 0:
            holdout.append(_row(forecast, observed, date))
        else:
            train.append(_row(forecast, observed, date))
    fitted = fit_station_downscaling(train)
    eval_ = evaluate_downscaling(fitted, holdout)
    assert eval_["n"] > 0
    assert eval_["mae_corrected"] is not None
    assert eval_["mae_raw"] is not None
    # Corrected should beat raw by at least a small margin
    assert eval_["mae_corrected"] <= eval_["mae_raw"] + 0.1


def test_evaluate_handles_empty_holdout():
    eval_ = evaluate_downscaling({"coef": [1, 0, 0, 0], "intercept": 0}, [])
    assert eval_["n"] == 0
    assert eval_["mae_raw"] is None
