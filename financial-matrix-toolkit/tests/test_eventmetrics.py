"""Tests for the probabilistic event metrics."""

import numpy as np

from eventmetrics import (
    average_precision,
    best_threshold,
    bootstrap_auc_ci,
    bootstrap_metric_ci,
    brier,
    brier_skill_score,
    ks_statistic,
    lift_at_k,
    lift_efficiency,
    log_loss,
    max_lift_at_k,
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


def test_cluster_bootstrap_is_wider_than_iid_when_rows_are_correlated():
    """The pipeline pools 15 correlated assets per origin. Resampling rows i.i.d.
    pretends they carry independent information and returns too NARROW a CI; the
    cluster bootstrap must be honest about that."""
    rng = np.random.default_rng(11)
    n_grp, per_grp = 120, 15
    groups, y, s = [], [], []
    for g in range(n_grp):
        shock = rng.normal(0, 1.2)                 # shared "market factor"
        yg = (rng.random(per_grp) < 0.4).astype(float)
        sg = yg * 0.7 + shock + rng.normal(0, 0.5, per_grp)   # same shock for all
        groups.append(np.full(per_grp, g)); y.append(yg); s.append(sg)
    groups = np.concatenate(groups); y = np.concatenate(y); s = np.concatenate(s)

    lo_i, hi_i = bootstrap_metric_ci(y, s, roc_auc, n_boot=400)
    lo_c, hi_c = bootstrap_metric_ci(y, s, roc_auc, groups=groups, n_boot=400)
    assert (hi_c - lo_c) > (hi_i - lo_i)           # honest CI is wider
    assert lo_c < roc_auc(y, s) < hi_c             # and still covers the estimate


def test_bootstrap_metric_ci_works_for_any_statistic():
    rng = np.random.default_rng(12)
    y = np.r_[np.zeros(300), np.ones(300)]
    s = np.r_[rng.normal(0, 1, 300), rng.normal(1.5, 1, 300)]
    lo, hi = bootstrap_metric_ci(y, s, roc_auc, n_boot=300)
    assert lo > 0.5                                # real skill detected

    # An UNINFORMATIVE BUT CALIBRATED forecast (always the base rate, jittered)
    # is the climatology null itself, so its BSS interval must include 0.
    p_flat = np.clip(y.mean() + rng.normal(0, 0.01, 600), 0.01, 0.99)
    lo_b, hi_b = bootstrap_metric_ci(y, p_flat, brier_skill_score, n_boot=300)
    assert lo_b < 0 < hi_b

    # Confident RANDOM probabilities are strictly worse than climatology: BSS is
    # negative with the whole interval below 0. Noise must never look like skill.
    p_noise = 1 / (1 + np.exp(-rng.normal(0, 1, 600)))
    lo_n, hi_n = bootstrap_metric_ci(y, p_noise, brier_skill_score, n_boot=300)
    assert hi_n < 0


def test_bootstrap_auc_ci_still_accepts_its_original_signature():
    rng = np.random.default_rng(13)
    y = np.r_[np.zeros(200), np.ones(200)]
    s = np.r_[rng.normal(0, 1, 200), rng.normal(2.0, 1, 200)]
    lo, hi = bootstrap_auc_ci(y, s, 300)           # positional n_boot, as before
    assert 0.5 < lo < hi <= 1.0
    g = np.tile(np.arange(40), 10)
    lo_g, hi_g = bootstrap_auc_ci(y, s, n_boot=300, groups=g)
    assert np.isfinite(lo_g) and np.isfinite(hi_g)


def test_lift_ceiling_makes_events_comparable():
    """Raw lift is capped at min(1/base_rate, 1/frac), so a COMMON event can look
    weak at its mathematical maximum while a rare event looks strong far below
    its own. lift_efficiency removes the base-rate dependence."""
    # common event: 80% base rate -> ceiling 1.25x
    y_common = np.r_[np.ones(80), np.zeros(20)]
    s_perfect = np.r_[np.linspace(1.0, 0.6, 80), np.linspace(0.4, 0.0, 20)]
    assert abs(max_lift_at_k(y_common, 0.10) - 1.25) < 1e-9
    assert abs(lift_at_k(y_common, s_perfect, 0.10) - 1.25) < 1e-9
    assert abs(lift_efficiency(y_common, s_perfect, 0.10) - 1.0) < 1e-9   # PERFECT

    # rare event: 10% base rate -> ceiling 10x; a 2.5x lift is only 25% of it
    y_rare = np.r_[np.ones(10), np.zeros(90)]
    assert abs(max_lift_at_k(y_rare, 0.10) - 10.0) < 1e-9
    s_partial = np.zeros(100)
    s_partial[:10] = np.r_[np.ones(3), np.zeros(7)]      # only 3 of 10 ranked top
    order = np.r_[np.linspace(1.0, 0.9, 3), np.linspace(0.1, 0.0, 7),
                  np.linspace(0.8, 0.2, 90)]
    eff = lift_efficiency(y_rare, order, 0.10)
    assert 0.0 < eff < 1.0
    # the headline point: raw lift 1.25x (common) BEATS raw lift here in efficiency
    assert lift_efficiency(y_common, s_perfect, 0.10) > eff


def test_effectiveness_metrics_handle_degenerate_input():
    one_class = np.ones(50)
    p = np.linspace(0.1, 0.9, 50)
    assert np.isnan(ks_statistic(one_class, p))
    assert np.isnan(brier_skill_score(one_class, p))           # no climatology variance
    assert mcc(one_class, np.zeros(50)) == 0.0
    assert np.isnan(lift_at_k(np.zeros(50), p))                # no positives -> no lift


def test_bootstrap_metric_ci_rejects_mismatched_groups():
    import pytest
    rng = np.random.default_rng(24)
    y = (rng.random(200) < 0.4).astype(float)
    s = rng.random(200)
    with pytest.raises(ValueError, match="one id per row"):
        bootstrap_metric_ci(y, s, roc_auc, groups=np.arange(50), n_boot=10)


def test_bootstrap_metric_ci_masks_groups_alongside_nans():
    """NaN rows are dropped from y/s; the group ids must be dropped in lockstep
    or every cluster would be misaligned."""
    rng = np.random.default_rng(25)
    n = 400
    y = (rng.random(n) < 0.4).astype(float)
    s = rng.random(n) + y * 0.6
    groups = np.repeat(np.arange(n // 10), 10)
    s[::7] = np.nan                                    # punch holes
    lo, hi = bootstrap_metric_ci(y, s, roc_auc, groups=groups, n_boot=200)
    assert np.isfinite(lo) and np.isfinite(hi) and lo < hi
    assert lo <= roc_auc(y, s) <= hi
