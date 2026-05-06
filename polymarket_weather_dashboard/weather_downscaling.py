"""Per-station forecast downscaling.

NWP models report a gridded value — typically the 2 m temperature at the
center of a 13 km cell. The actual resolution station can sit a few miles
off the cell center, on a different surface (concrete vs grass), or in a
microclimate the grid doesn't resolve (urban heat island, sea breeze,
cold-air drainage). Empirically, persistent biases of 1–3 °F at a single
airport are normal.

The bias-correction loop in server.py already removes the *mean* error
per (station, model) pair. What it doesn't capture:

  * Conditional biases — models often run hot at long lead and cold at
    short lead, or hotter on clear days than cloudy days.
  * Cross-station structure — stations in the same micro-region tend to
    err in the same direction; pooling helps when one station's pairing
    history is sparse.
  * Lead-time-dependent residuals — covered separately by the
    leadtime_sigma fit but worth blending into the mean correction too.

This module exposes a feature-rich linear regression with regularization
(sklearn-free; tiny on purpose). When fewer than 20 paired rows exist
the API gracefully falls back to "use the simple bias", so callers can
always invoke `downscale(forecast, ...)` without checking first.

Model: ``observed = β0 + β1 * forecast + β2 * lead_days + β3 * sin(doy)
+ β4 * cos(doy)`` fit by ridge least-squares (λ = 1.0 by default). The
β1 term lets the regression learn the "model runs 5% too hot in summer"
shape that a constant-offset bias misses.
"""

from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

MIN_ROWS_FOR_FIT = 20


def _doy_features(target_date_iso: str) -> tuple[float, float]:
    """Return ``(sin, cos)`` of day-of-year encoded on a 365-period.

    Used as continuous seasonal predictors so the regression doesn't have
    to memorize 12 monthly intercepts. Returns ``(0, 0)`` when the date
    fails to parse so the model is robust to garbage input.
    """
    try:
        dt = datetime.strptime(target_date_iso[:10], "%Y-%m-%d")
        doy = dt.timetuple().tm_yday
    except (ValueError, TypeError):
        return (0.0, 0.0)
    angle = 2.0 * math.pi * doy / 365.0
    return (math.sin(angle), math.cos(angle))


def _matrix_solve_ridge(rows: list[list[float]], y: list[float],
                        lam: float = 1.0) -> Optional[list[float]]:
    """Closed-form ridge regression. ``rows`` is the design matrix
    (each row one observation, features include the bias column),
    ``y`` is the target vector. Returns the coefficient vector or
    ``None`` if the normal equations are singular.

    Implemented in pure Python so the module has no numpy/scipy
    dependency at import time — keeps the dashboard's deploy footprint
    small and the unit tests fast.
    """
    if not rows or not y or len(rows) != len(y):
        return None
    n = len(rows)
    p = len(rows[0])
    # XᵀX
    xtx = [[0.0] * p for _ in range(p)]
    for r in rows:
        for i in range(p):
            ri = r[i]
            for j in range(p):
                xtx[i][j] += ri * r[j]
    # XᵀX + λI (skip the bias column so the intercept isn't shrunk)
    for i in range(p):
        if i > 0:
            xtx[i][i] += lam
    # Xᵀy
    xty = [0.0] * p
    for r, yi in zip(rows, y):
        for i in range(p):
            xty[i] += r[i] * yi
    # Solve via Gauss-Jordan with partial pivoting
    aug = [row + [xty[i]] for i, row in enumerate(xtx)]
    for col in range(p):
        pivot_row = max(range(col, p), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot_row][col]) < 1e-12:
            return None
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        pv = aug[col][col]
        aug[col] = [v / pv for v in aug[col]]
        for r in range(p):
            if r == col:
                continue
            factor = aug[r][col]
            aug[r] = [aug[r][i] - factor * aug[col][i] for i in range(p + 1)]
    return [row[-1] for row in aug]


def fit_station_downscaling(rows: list[dict],
                            ridge_lambda: float = 1.0) -> dict:
    """Fit a downscaling regression from forecast_history pairs.

    Each input row needs ``forecast_high``, ``observed_high``,
    ``target_date``, and ideally ``lead_days`` (we treat missing leads
    as 1). Returns ``{coef, intercept, r2, n, source, residual_std}``.
    Falls back to a constant-bias model when the regression is rank
    deficient or when fewer than `MIN_ROWS_FOR_FIT` paired rows are
    available — that way the caller always gets a usable correction.
    """
    cleaned: list[tuple[float, float, float, float, float]] = []
    for r in rows:
        f = r.get("forecast_high")
        o = r.get("observed_high")
        t = r.get("target_date") or ""
        ld = r.get("lead_days", 1)
        if f is None or o is None:
            continue
        try:
            f = float(f)
            o = float(o)
            ld = float(ld) if ld is not None else 1.0
        except (TypeError, ValueError):
            continue
        sin_doy, cos_doy = _doy_features(t)
        cleaned.append((f, o, ld, sin_doy, cos_doy))

    if len(cleaned) < MIN_ROWS_FOR_FIT:
        # Fall back to mean-bias correction
        if not cleaned:
            return {"coef": None, "intercept": None, "r2": None, "n": 0,
                    "source": "empty", "residual_std": None}
        residuals = [o - f for (f, o, _ld, _s, _c) in cleaned]
        mean_bias = statistics.mean(residuals)
        std = statistics.stdev(residuals) if len(residuals) > 1 else None
        return {
            "coef": [1.0, 0.0, 0.0, 0.0],
            "intercept": round(mean_bias, 3),
            "r2": None,
            "n": len(cleaned),
            "source": "mean_bias",
            "residual_std": round(std, 3) if std is not None else None,
        }

    # Design matrix: bias, forecast, lead, sin(doy), cos(doy)
    X = [[1.0, f, ld, s, c] for (f, _o, ld, s, c) in cleaned]
    y = [o for (_f, o, _ld, _s, _c) in cleaned]
    beta = _matrix_solve_ridge(X, y, lam=ridge_lambda)
    if beta is None:
        residuals = [o - f for (f, o, _ld, _s, _c) in cleaned]
        mean_bias = statistics.mean(residuals)
        std = statistics.stdev(residuals) if len(residuals) > 1 else None
        return {
            "coef": [1.0, 0.0, 0.0, 0.0],
            "intercept": round(mean_bias, 3),
            "r2": None,
            "n": len(cleaned),
            "source": "mean_bias_singular",
            "residual_std": round(std, 3) if std is not None else None,
        }

    intercept, b_f, b_l, b_s, b_c = beta
    fitted = [intercept + b_f * x[1] + b_l * x[2] + b_s * x[3] + b_c * x[4]
              for x in X]
    residuals = [yi - yhat for yi, yhat in zip(y, fitted)]
    ss_res = sum(r * r for r in residuals)
    mean_y = statistics.mean(y)
    ss_tot = sum((yi - mean_y) ** 2 for yi in y)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else None
    residual_std = statistics.stdev(residuals) if len(residuals) > 1 else None
    return {
        "coef": [round(b_f, 4), round(b_l, 4), round(b_s, 4), round(b_c, 4)],
        "intercept": round(intercept, 3),
        "r2": round(r2, 4) if r2 is not None else None,
        "n": len(cleaned),
        "source": "ridge",
        "residual_std": round(residual_std, 3) if residual_std is not None else None,
    }


def apply_downscaling(model: dict, raw_forecast: float, target_date_iso: str,
                      lead_days: float = 1.0) -> Optional[float]:
    """Apply a fitted downscaling model to a single forecast value.

    Returns the corrected °F prediction, or None if the model is empty.
    Clamps the correction to ±15 °F so a pathological fit on noisy data
    can't produce a 50 °F adjustment that would fire spurious signals.
    """
    if not model or model.get("coef") is None or model.get("intercept") is None:
        return None
    if raw_forecast is None:
        return None
    try:
        raw = float(raw_forecast)
    except (TypeError, ValueError):
        return None
    sin_doy, cos_doy = _doy_features(target_date_iso)
    coef = model["coef"]
    if len(coef) != 4:
        return None
    intercept = float(model["intercept"])
    corrected = (intercept + coef[0] * raw + coef[1] * float(lead_days)
                 + coef[2] * sin_doy + coef[3] * cos_doy)
    delta = corrected - raw
    if abs(delta) > 15.0:
        # Clamp pathological corrections; surface the clamp in the result
        corrected = raw + (15.0 if delta > 0 else -15.0)
    return round(corrected, 2)


def evaluate_downscaling(model: dict, raw: list[dict]) -> dict:
    """Out-of-sample evaluation helper: pass in a holdout list of dicts
    with ``forecast_high``, ``observed_high``, ``target_date``,
    ``lead_days``. Returns ``{mae_raw, mae_corrected, improvement_pct}``.
    Useful for the calibration page."""
    if not raw:
        return {"n": 0, "mae_raw": None, "mae_corrected": None,
                "improvement_pct": None}
    raws, corrs, obss = [], [], []
    for r in raw:
        f = r.get("forecast_high")
        o = r.get("observed_high")
        t = r.get("target_date") or ""
        ld = r.get("lead_days", 1)
        if f is None or o is None:
            continue
        c = apply_downscaling(model, f, t, ld) if model else None
        raws.append(abs(float(f) - float(o)))
        obss.append(o)
        if c is not None:
            corrs.append(abs(c - float(o)))
        else:
            corrs.append(abs(float(f) - float(o)))
    if not raws:
        return {"n": 0, "mae_raw": None, "mae_corrected": None,
                "improvement_pct": None}
    mae_raw = statistics.mean(raws)
    mae_corr = statistics.mean(corrs)
    improvement = (mae_raw - mae_corr) / mae_raw if mae_raw > 0 else 0.0
    return {
        "n": len(raws),
        "mae_raw": round(mae_raw, 3),
        "mae_corrected": round(mae_corr, 3),
        "improvement_pct": round(improvement * 100, 2),
    }
