"""Tests for the risk-managed backtest (backtest.py)."""

import numpy as np

from backtest import (
    block_bootstrap_sharpe_diff,
    ewma_cov_series,
    perf,
    run_strategy,
    target_weights,
)
from data import load_market_data


def test_ewma_cov_is_causal():
    md = load_market_data()
    R = md.R.copy()
    covs = ewma_cov_series(R)
    R2 = R.copy()
    R2[60:] = 0.0                       # scramble the FUTURE
    covs2 = ewma_cov_series(R2)
    # cov[t] for t <= 60 must be untouched by future returns -> no look-ahead
    assert np.allclose(covs[:61], covs2[:61])


def test_buy_hold_is_equal_weight_mean():
    md = load_market_data()
    R = md.R
    covs = ewma_cov_series(R)
    net, gross, to, W = run_strategy(R, covs, "buy_hold_ew", 0.12, 1, 0.0, 126)
    assert np.allclose(gross, R[126:].mean(axis=1))
    assert np.allclose(to, 0.0)          # constant equal weights => no turnover


def test_risk_parity_weights_sum_to_one():
    md = load_market_data()
    W = target_weights(ewma_cov_series(md.R), "inverse_vol", 0.12)
    assert np.allclose(W.sum(axis=1), 1.0)
    assert np.all(W >= 0)


def test_vol_targeting_reduces_realised_vol():
    md = load_market_data()
    R = md.R
    covs = ewma_cov_series(R)
    bh = perf(run_strategy(R, covs, "buy_hold_ew", 0.12, 5, 0.0, 126)[0])
    vm = perf(run_strategy(R, covs, "vol_managed", 0.08, 5, 0.0, 126)[0])
    assert vm["ann_vol"] < bh["ann_vol"]


def test_perf_and_bootstrap_wellformed():
    r = np.random.default_rng(0).normal(0.0004, 0.01, 600)
    p = perf(r)
    assert {"sharpe", "max_dd", "cagr", "ann_vol"} <= set(p)
    assert p["max_dd"] <= 0
    lo, hi = block_bootstrap_sharpe_diff(r, r * 0.9 + 1e-5)
    assert lo <= hi
