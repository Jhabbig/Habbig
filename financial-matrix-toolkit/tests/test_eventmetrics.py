"""Tests for the probabilistic event metrics."""

import numpy as np

from eventmetrics import (
    average_precision,
    best_threshold,
    bootstrap_auc_ci,
    brier,
    prob_metrics,
    roc_auc,
)


def test_roc_auc_perfect_and_reversed():
    y = np.array([0, 0, 1, 1])
    assert roc_auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert roc_auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0


def test_roc_auc_chance_is_half():
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, 4000).astype(float)
    s = rng.normal(0, 1, 4000)          # independent of y
    assert abs(roc_auc(y, s) - 0.5) < 0.05


def test_average_precision_ranges():
    y = np.array([1, 1, 0, 0])
    assert average_precision(y, np.array([0.9, 0.8, 0.2, 0.1])) == 1.0   # perfect ranking
    # a rare positive still gives AP >= base rate for a good ranker
    y2 = np.array([0, 0, 0, 1])
    assert average_precision(y2, np.array([0.1, 0.2, 0.3, 0.9])) == 1.0


def test_brier_bounds():
    y = np.array([1.0, 0.0])
    assert brier(y, np.array([1.0, 0.0])) == 0.0
    assert brier(y, np.array([0.0, 1.0])) == 1.0


def test_best_threshold_separates():
    y = np.array([0, 0, 0, 1, 1, 1], dtype=float)
    s = np.array([0.1, 0.2, 0.3, 0.7, 0.8, 0.9])
    t = best_threshold(y, s, metric="bal")
    pred = s >= t
    assert np.array_equal(pred.astype(float), y)   # threshold perfectly splits


def test_bootstrap_ci_detects_real_skill():
    rng = np.random.default_rng(1)
    y = np.r_[np.zeros(300), np.ones(300)]
    s = np.r_[rng.normal(0, 1, 300), rng.normal(1.5, 1, 300)]  # separated
    lo, hi = bootstrap_auc_ci(y, s, n_boot=300)
    assert lo > 0.5 and hi <= 1.0


def test_bootstrap_ci_includes_half_for_noise():
    rng = np.random.default_rng(2)
    y = rng.integers(0, 2, 400).astype(float)
    s = rng.normal(0, 1, 400)
    lo, hi = bootstrap_auc_ci(y, s, n_boot=300)
    assert lo < 0.5 < hi                # CI straddles chance


def test_prob_metrics_bundle_keys():
    y = np.array([0, 1, 0, 1], dtype=float)
    p = np.array([0.2, 0.8, 0.3, 0.7])
    m = prob_metrics(y, p, threshold=0.5)
    assert {"auc", "ap", "brier", "bal_acc", "precision", "recall", "f1"} <= set(m)
