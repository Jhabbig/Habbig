"""Tests for the two-stage pipeline (track models -> event readout).

Includes a leakage guard: purely random track features must NOT produce skill
(balanced accuracy stays ~0.5), while a genuinely predictive feature is learned.
"""

import numpy as np

from data import load_market_data
from pipeline import TRACK_FEATURES, build_tracks, readout_event, _fit_final
from predict_events import predict_logistic


def _fake(n_orig=60, n=6, stride=5, seed=0):
    rng = np.random.default_rng(seed)
    origins = list(range(80, 80 + n_orig * stride, stride))
    T = origins[-1] + 3
    S = np.full((T, n), np.nan)
    for t in origins:
        S[t + 1] = rng.integers(0, 2, n).astype(float)
    return origins, S, T, n, rng


def test_build_tracks_shapes():
    md = load_market_data()
    origins, X, last = build_tracks(md, window=300, stride=40, hmm_iter=8, verbose=False)
    assert len(origins) > 3
    assert X.shape == (len(origins), md.n, len(TRACK_FEATURES))
    assert np.isfinite(X).all()
    assert set(last) == {"ewma", "rc", "hmm", "pca", "rmt", "dmd"}


def test_readout_learns_predictive_feature():
    origins, S, T, n, rng = _fake(seed=1)
    X = rng.normal(0, 1, (len(origins), n, 4))
    for i, t in enumerate(origins):
        X[i, :, 0] = S[t + 1]          # feature 0 carries the label -> learnable
    res = readout_event(origins, X, S, horizon=1, stride=5, min_train=20)
    assert res["bal_acc"] > 0.9


def test_readout_no_skill_on_noise():
    # The leakage guard: features independent of the label must give ~0.5.
    origins, S, T, n, rng = _fake(seed=2)
    X = rng.normal(0, 1, (len(origins), n, 5))
    res = readout_event(origins, X, S, horizon=1, stride=5, min_train=20)
    assert abs(res["bal_acc"] - 0.5) < 0.12


def test_readout_effectiveness_metrics_reject_noise():
    """Noise features must not be credited with EFFECTIVENESS either: the Brier
    skill CI has to include 0 so the reported verdict cannot be 'significant'."""
    origins, S, T, n, rng = _fake(seed=4)
    X = rng.normal(0, 1, (len(origins), n, 5))
    res = readout_event(origins, X, S, horizon=1, stride=5, min_train=20)
    assert res["bss_lo"] <= 0.0 <= res["bss_hi"]      # cannot claim skill on noise
    assert abs(res["mcc_tuned"]) < 0.25


def test_readout_ci_is_clustered_by_origin():
    """The CI must be a cluster bootstrap over origins, not over pooled rows.
    A learnable signal still has to come out significant under clustering."""
    origins, S, T, n, rng = _fake(seed=5)
    X = rng.normal(0, 1, (len(origins), n, 4))
    for i, t in enumerate(origins):
        X[i, :, 0] = S[t + 1]
    res = readout_event(origins, X, S, horizon=1, stride=5, min_train=20)
    assert res["n_origins"] > 1                       # groups were actually tracked
    assert res["n_origins"] < len(res["y"])           # ...and coarser than the rows
    assert res["bss_lo"] > 0.0                        # real signal survives clustering


def test_effectiveness_fields_needed_by_predict_live_are_present():
    """predict_live.py gates its 'safe to size on' verdict on these keys."""
    origins, S, T, n, rng = _fake(seed=6)
    X = rng.normal(0, 1, (len(origins), n, 4))
    for i, t in enumerate(origins):
        X[i, :, 0] = S[t + 1]
    res = readout_event(origins, X, S, horizon=1, stride=5, min_train=20)
    for key in ("bss_cal", "bss_lo", "bss_hi", "mcc_tuned", "ks",
                "lift_at_10", "max_lift_at_10", "lift_efficiency_10",
                "spieg_z", "spieg_p", "n_origins"):
        assert key in res, f"missing {key}"
        assert np.isfinite(res[key]), f"{key} is not finite"
    # lift can never exceed its own ceiling
    assert res["lift_at_10"] <= res["max_lift_at_10"] + 1e-9
    assert 0.0 <= res["lift_efficiency_10"] <= 1.0 + 1e-9


def test_saved_readout_roundtrip():
    origins, S, T, n, rng = _fake(seed=3)
    X = rng.normal(0, 1, (len(origins), n, 6))
    for i, t in enumerate(origins):
        X[i, :, 0] = S[t + 1]
    w, mu, sd, cal = _fit_final(X, origins, S, horizon=1)
    assert w is not None and cal is not None
    pred = predict_logistic(w, mu, sd, X[0])
    assert pred.shape == (n,)
    assert set(np.unique(pred)).issubset({0.0, 1.0})
