"""Tests for the probabilistic event metrics."""

import numpy as np

from eventmetrics import (
    average_precision,
    best_threshold,
    bootstrap_auc_ci,
    brier,
    brier_skill_score,
    ks_statistic,
    lift_at_k,
    log_loss,
    mcc,
    murphy_decomposition,
    precision_at_k,
    prob_metrics,
    roc_auc,
    spiegelhalter_z,
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
    assert {"auc", "ap", "brier", "bal_acc", "precision", "recall", "f1",
            "log_loss", "bss", "mcc", "ks", "precision_at_10", "lift_at_10"} <= set(m)


# --------------------------------------------------------------------------- #
# effectiveness metrics
# --------------------------------------------------------------------------- #
def test_log_loss_rewards_confidence_only_when_right():
    y = np.array([1.0, 0.0])
    assert log_loss(y, np.array([0.99, 0.01])) < 0.05          # confident + right
    assert log_loss(y, np.array([0.01, 0.99])) > 4.0           # confident + wrong
    # log loss punishes the confident miss harder than Brier does
    assert log_loss(y, np.array([0.01, 0.99])) > brier(y, np.array([0.01, 0.99]))


def test_brier_skill_score_anchored_to_climatology():
    rng = np.random.default_rng(3)
    y = (rng.random(2000) < 0.2).astype(float)                 # 20% base rate
    base = np.full(2000, y.mean())
    assert abs(brier_skill_score(y, base)) < 1e-9              # climatology itself = 0
    assert brier_skill_score(y, y) == 1.0                      # perfect = 1
    assert brier_skill_score(y, 1 - y) < 0                     # anti-skill < 0
    # an explicit (training) base rate can be passed as the reference
    assert brier_skill_score(y, base, base_rate=0.5) > 0       # beats a WORSE null


def test_murphy_decomposition_identity_and_bounds():
    rng = np.random.default_rng(4)
    # forecasts constant within bins -> Brier = REL - RES + UNC holds exactly
    p = rng.choice([0.05, 0.35, 0.65, 0.95], size=4000)
    y = (rng.random(4000) < p).astype(float)                   # perfectly calibrated
    d = murphy_decomposition(y, p, n_bins=10)
    b = brier(y, p)
    assert abs((d["reliability"] - d["resolution"] + d["uncertainty"]) - b) < 1e-9
    assert d["reliability"] < 0.005                            # honest forecaster
    assert d["resolution"] > 0.05                              # informative forecaster
    assert abs(d["uncertainty"] - y.mean() * (1 - y.mean())) < 1e-12
    # a constant forecast has zero resolution
    d0 = murphy_decomposition(y, np.full(4000, y.mean()), n_bins=10)
    assert d0["resolution"] < 1e-12


def test_spiegelhalter_z_flags_dishonest_probabilities():
    rng = np.random.default_rng(5)
    p = rng.uniform(0.05, 0.95, 3000)
    y = (rng.random(3000) < p).astype(float)                   # calibrated by construction
    z, pv = spiegelhalter_z(y, p)
    assert abs(z) < 3.0 and pv > 0.001                         # cannot reject honesty
    # overconfident forecaster: says 0.9/0.1 when truth is 0.6/0.4
    p_over = np.where(p > 0.5, 0.9, 0.1)
    y2 = (rng.random(3000) < np.where(p > 0.5, 0.6, 0.4)).astype(float)
    z2, pv2 = spiegelhalter_z(y2, p_over)
    assert abs(z2) > 3.0 and pv2 < 0.01                        # clearly dishonest


def test_mcc_zero_for_base_rate_guessing():
    y = np.r_[np.ones(10), np.zeros(90)]                       # rare event
    always_no = np.zeros(100)
    assert mcc(y, always_no) == 0.0                            # 90% accurate, 0 skill
    assert mcc(y, y) == 1.0                                    # perfect
    assert mcc(y, 1 - y) == -1.0                               # perfectly wrong


def test_ks_statistic_separation():
    y = np.r_[np.zeros(200), np.ones(200)]
    s = np.r_[np.linspace(0, 0.4, 200), np.linspace(0.6, 1.0, 200)]
    assert ks_statistic(y, s) == 1.0                           # fully separable
    rng = np.random.default_rng(6)
    assert ks_statistic(rng.integers(0, 2, 2000), rng.normal(size=2000)) < 0.1


def test_precision_and_lift_at_k():
    # 10% base rate, perfect ranker: the top decile is exactly the events
    y = np.r_[np.ones(10), np.zeros(90)]
    s = np.r_[np.linspace(0.9, 1.0, 10), np.linspace(0.0, 0.5, 90)]
    assert precision_at_k(y, s, frac=0.10) == 1.0
    assert abs(lift_at_k(y, s, frac=0.10) - 10.0) < 1e-9       # 1 / base_rate
    # random scores: precision ~ base rate, lift ~ 1
    rng = np.random.default_rng(7)
    yr = (rng.random(5000) < 0.10).astype(float)
    sr = rng.random(5000)
    assert abs(lift_at_k(yr, sr, frac=0.10) - 1.0) < 0.35


def test_effectiveness_metrics_handle_degenerate_input():
    one_class = np.ones(50)
    p = np.linspace(0.1, 0.9, 50)
    assert np.isnan(ks_statistic(one_class, p))
    assert np.isnan(brier_skill_score(one_class, p))           # no climatology variance
    assert mcc(one_class, np.zeros(50)) == 0.0
    assert np.isnan(lift_at_k(np.zeros(50), p))                # no positives -> no lift
