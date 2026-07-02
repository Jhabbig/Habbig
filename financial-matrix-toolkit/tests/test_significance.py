"""Tests for the after-cost edge significance machinery.

These guard the fix that stops the toolkit from crowning small-sample noise as a
direction "edge": a point-estimate win over the null is not enough - the edge
must also pass a paired Newey-West t-test.
"""

import numpy as np

from harness import _newey_west_tstat, _transaction_cost_report


def test_nw_pure_noise_not_significant():
    rng = np.random.default_rng(0)
    d = rng.normal(0, 1, 500)          # zero-mean noise
    t, p = _newey_west_tstat(d)
    assert p > 0.05                    # cannot reject "no edge"


def test_nw_strong_signal_significant():
    rng = np.random.default_rng(1)
    d = 0.5 + rng.normal(0, 1, 500)    # large consistent positive mean
    t, p = _newey_west_tstat(d)
    assert t > 0 and p < 0.01


def test_nw_small_positive_within_noise():
    rng = np.random.default_rng(2)
    d = 0.02 + rng.normal(0, 1, 200)   # tiny positive mean, swamped by variance
    t, p = _newey_west_tstat(d)
    assert p > 0.05                    # a positive point estimate that is NOT significant


def test_nw_corrects_autocorrelation():
    # Positively autocorrelated noise inflates a naive t-stat; Newey-West should
    # give a larger SE (smaller |t|) than the iid formula on the same data.
    rng = np.random.default_rng(3)
    e = rng.normal(0, 1, 600)
    d = e.copy()
    for i in range(1, len(d)):
        d[i] += 0.7 * d[i - 1]         # strong AR(1)
    t_nw, _ = _newey_west_tstat(d, lag=10)
    naive_se = d.std() / np.sqrt(len(d))
    t_naive = d.mean() / naive_se
    assert abs(t_nw) <= abs(t_naive) + 1e-9


def _series(W, n, seed):
    return np.random.default_rng(seed).normal(0, 0.01, (W, n))


def test_cost_report_genuine_edge_is_significant():
    # A model that always knows the sign (perfect) vs an always-long null on
    # symmetric returns: a strong, consistent, significant edge.
    actual = _series(250, 5, 10)
    pred = np.sign(actual)             # perfect (for accounting test only)
    null = np.ones_like(actual)        # always long
    rep = _transaction_cost_report(pred, actual, null, cost_bps=0.0)
    assert rep["beats_null_pointest"] is True
    assert rep["edge_significant"] is True
    assert rep["edge_below_cost"] is False


def test_cost_report_no_edge_when_equal_to_null():
    actual = _series(250, 5, 11)
    null = np.ones_like(actual)
    pred = np.ones_like(actual)        # identical to the null -> no edge at all
    rep = _transaction_cost_report(pred, actual, null, cost_bps=10.0)
    assert rep["beats_null_pointest"] is False
    assert rep["edge_below_cost"] is True


def test_cost_report_random_predictions_not_significant():
    # Predictions independent of the future cannot have a real edge: even if a
    # lucky run beats the null point estimate, it must not be flagged significant.
    actual = _series(250, 5, 12)
    null = np.ones_like(actual)
    pred = np.sign(np.random.default_rng(99).normal(0, 1, actual.shape))
    rep = _transaction_cost_report(pred, actual, null, cost_bps=10.0)
    assert rep["edge_significant"] is False
