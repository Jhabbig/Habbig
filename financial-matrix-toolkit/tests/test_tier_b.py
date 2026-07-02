"""Per-model tests for Tier B regime / state models."""

import numpy as np
import pytest

from core import TARGET_DIRECTION, TARGET_PRICE, TARGET_RETURNS, TARGET_VARIANCE
from models.tier_b_regime import (
    AbsorbingDrawdownChain,
    DMDReturns,
    GaussianHMMRegime,
    KalmanLocalLevel,
    MarkovChainBuckets,
    MarkovSwitchingMeanVar,
)

FORECASTERS = [
    (lambda: GaussianHMMRegime(n_iter=20), TARGET_VARIANCE),
    (lambda: MarkovSwitchingMeanVar(n_iter=20), TARGET_VARIANCE),
    (lambda: MarkovChainBuckets(), TARGET_DIRECTION),
    (lambda: DMDReturns(rank=4), TARGET_RETURNS),
    (lambda: KalmanLocalLevel(), TARGET_PRICE),
]


@pytest.mark.parametrize("factory,target", FORECASTERS)
def test_fit_predict_shape(factory, target, tiny_md):
    m = factory().fit(tiny_md)
    assert m.target == target
    for steps in (1, 3):
        pred = m.predict(steps)
        assert pred.shape == (steps, tiny_md.n)
        assert np.all(np.isfinite(pred))


@pytest.mark.parametrize("factory,target", FORECASTERS)
def test_state_populated(factory, target, tiny_md):
    m = factory().fit(tiny_md)
    st = m.state()
    assert isinstance(st, dict) and len(st) > 0


def test_hmm_identifies_two_regimes(tiny_md):
    m = GaussianHMMRegime(n_states=2, n_iter=40).fit(tiny_md)
    st = m.state()
    vols = st["state_volatility_market"]
    assert vols[1] > vols[0]                  # crisis vol > calm vol
    assert 0 <= st["crisis_fraction"] <= 1
    assert st["persistence"] > 0.5            # regimes are persistent


def test_variance_forecasts_positive(tiny_md):
    for f in (GaussianHMMRegime(n_iter=20), MarkovSwitchingMeanVar(n_iter=20)):
        f.fit(tiny_md)
        assert np.all(f.predict(1) > 0)


def test_markov_buckets_signs(tiny_md):
    m = MarkovChainBuckets().fit(tiny_md)
    pred = m.predict(1)
    assert set(np.unique(pred)).issubset({-1.0, 1.0})


def test_absorbing_chain_recovery(tiny_md):
    m = AbsorbingDrawdownChain().fit(tiny_md)
    assert m.target is None
    with pytest.raises(NotImplementedError):
        m.predict(1)
    rec = m.expected_time_to_recovery()
    assert rec >= 0
    assert "expected_recovery_days_by_depth" in m.state()


def test_kalman_tracks_price(tiny_md):
    m = KalmanLocalLevel().fit(tiny_md)
    level = m.state()["filtered_level"]
    # filtered level should be close to the last observed price
    assert np.allclose(level, tiny_md.P[-1], rtol=0.1)


def test_dmd_operator_shape(tiny_md):
    m = DMDReturns(rank=4).fit(tiny_md)
    assert m._A.shape == (tiny_md.n, tiny_md.n)
    assert "spectral_radius" in m.state()
