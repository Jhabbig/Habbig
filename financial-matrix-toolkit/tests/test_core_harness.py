"""Tests for core nulls, metrics, and the walk-forward harness."""

import numpy as np

from core import (
    directional_accuracy,
    null_base_rate_direction,
    null_random_walk_price,
    null_rolling_mean_variance,
    null_zero_return,
    qlike,
    rmse,
    skill,
)
from harness import slice_market_data, walk_forward
from models.tier_c_volatility import EWMAVolatility


def test_metrics_basic():
    assert rmse([1, 2, 3], [1, 2, 3]) == 0.0
    assert rmse([0, 0], [1, 1]) == 1.0
    assert skill(0.5, 1.0) == 0.5          # halved error -> 50% skill
    assert skill(1.0, 1.0) == 0.0
    assert np.isnan(skill(1.0, 0.0))


def test_qlike_minimised_by_truth():
    rng = np.random.default_rng(0)
    true_var = 0.0004
    realized = rng.standard_normal(20000) ** 2 * true_var
    good = qlike(np.full_like(realized, true_var), realized)
    bad = qlike(np.full_like(realized, true_var * 4), realized)
    assert good < bad


def test_nulls_shapes():
    P = np.cumprod(1 + 0.01 * np.ones((50, 4)), axis=0) * 100
    R = np.diff(np.log(P), axis=0)
    assert null_random_walk_price(P, 3).shape == (3, 4)
    assert null_zero_return(4, 2).shape == (2, 4)
    assert np.all(null_zero_return(4, 2) == 0)
    assert null_base_rate_direction(R, 1).shape == (1, 4)
    assert set(np.unique(null_base_rate_direction(R, 1))).issubset({-1.0, 1.0})
    assert null_rolling_mean_variance(R, 10, 2).shape == (2, 4)


def test_directional_accuracy_perfect_and_coinflip():
    actual = np.array([0.01, -0.02, 0.03, -0.01])
    assert directional_accuracy(np.sign(actual), actual) == 1.0
    assert directional_accuracy(-np.sign(actual), actual) == 0.0


def test_slice_market_data_consistency(tiny_md):
    sl = slice_market_data(tiny_md, 10, 40)
    assert sl.R.shape[0] == 30
    assert sl.P.shape[0] == 31  # R[a:b] needs P[a:b+1]
    assert sl.n == tiny_md.n


def test_walk_forward_runs_and_beats_null(tiny_md):
    res = walk_forward(lambda: EWMAVolatility(0.94), tiny_md, train_window=150, step=10)
    assert res.n_windows > 0
    assert np.isfinite(res.error) and np.isfinite(res.null_error)
    # EWMA should beat the rolling-mean variance null on vol-clustered data.
    assert res.skill > 0


def test_walk_forward_rejects_decomposition(tiny_md):
    # A fake decomposition-style model (target None) must be rejected.
    class Decomp(EWMAVolatility):
        target = None

    import pytest

    with pytest.raises(ValueError):
        walk_forward(lambda: Decomp(), tiny_md, train_window=150)
