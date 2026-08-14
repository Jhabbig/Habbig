"""Calibration metrics for backtest rows.

Given the projected-vs-actual rows from each backtest, summarise the model's
recent forecasting performance: mean absolute error (the headline number on
each card), RMSE, and bias (signed mean error — positive = over-projection).
"""
from __future__ import annotations

import math
from typing import Optional


def summary(rows: list[dict], error_key: str, unit: str) -> Optional[dict]:
    if not rows:
        return None
    errors = [r[error_key] for r in rows if error_key in r]
    if not errors:
        return None
    n = len(errors)
    mae = sum(abs(e) for e in errors) / n
    bias = sum(errors) / n
    rmse = math.sqrt(sum(e * e for e in errors) / n)
    return {
        "n": n,
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "bias": round(bias, 3),
        "unit": unit,
    }
