"""Tests for the general h-step forecaster (forecast.py).

Locks in the core lesson: a persistent macro-like series is genuinely
forecastable (an AR model beats no-change), while a driftless random-walk price
is not (no model beats no-change).
"""

import numpy as np

from core import rmse, skill
from forecast import MODELS, demo_series, walk_forward_1d


def _skill_vs_rw(y, h, model_name, train_min=250, step=1):
    rw_pred, rw_act, _ = walk_forward_1d(y, h, MODELS["RandomWalk(null)"], train_min, step)
    p, a, _ = walk_forward_1d(y, h, MODELS[model_name], train_min, step)
    return skill(rmse(p, a), rmse(rw_pred, rw_act))


def test_macro_series_is_forecastable():
    y = demo_series("macro").to_numpy(dtype=float)
    sk = _skill_vs_rw(y, 6, "RidgeAR-change")
    assert sk > 0.02  # persistent/mean-reverting -> AR genuinely beats no-change


def test_driftless_price_is_not_forecastable():
    y = demo_series("oil").to_numpy(dtype=float)
    # No AR variant should beat the no-change null on a driftless random walk.
    for m in ("RidgeAR-level", "RidgeAR-change"):
        assert _skill_vs_rw(y, 21, m) <= 0.02


def test_random_walk_null_is_self_consistent():
    y = demo_series("oil").to_numpy(dtype=float)
    assert _skill_vs_rw(y, 21, "RandomWalk(null)") == 0.0


def test_models_return_finite_scalars():
    y = demo_series("macro").to_numpy(dtype=float)
    for name, fn in MODELS.items():
        out = fn(y[:300], 6)
        assert np.isfinite(out)
