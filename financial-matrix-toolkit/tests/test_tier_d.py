"""Per-model tests for Tier D direction models (the honesty tier)."""

import numpy as np
import pytest

from core import TARGET_DIRECTION, TARGET_RETURNS
from models.tier_d_direction import (
    EchoStateNetwork,
    GPReturns,
    LogisticDirection,
    VARReturns,
)

TIER_D = [
    (lambda: LogisticDirection(epochs=100), TARGET_DIRECTION),
    (lambda: VARReturns(), TARGET_RETURNS),
    (lambda: GPReturns(max_train=100), TARGET_RETURNS),
    (lambda: EchoStateNetwork(n_reservoir=60), TARGET_RETURNS),
]


@pytest.mark.parametrize("factory,target", TIER_D)
def test_fit_predict_shape(factory, target, tiny_md):
    m = factory().fit(tiny_md)
    assert m.target == target
    for steps in (1, 3):
        pred = m.predict(steps)
        assert pred.shape == (steps, tiny_md.n)
        assert np.all(np.isfinite(pred))


@pytest.mark.parametrize("factory,target", TIER_D)
def test_state_populated(factory, target, tiny_md):
    m = factory().fit(tiny_md)
    st = m.state()
    assert isinstance(st, dict) and len(st) > 0


def test_logistic_outputs_signs(tiny_md):
    m = LogisticDirection(epochs=100).fit(tiny_md)
    pred = m.predict(1)
    assert set(np.unique(pred)).issubset({-1.0, 1.0})
    p = m.state()["current_up_probability"]
    assert np.all((p >= 0) & (p <= 1))


def test_var_returns_small_forecast(tiny_md):
    # A sane VAR forecast should be on the order of daily returns, not huge.
    m = VARReturns().fit(tiny_md)
    pred = m.predict(1)
    assert np.all(np.abs(pred) < 0.2)


def test_esn_reservoir_stable(tiny_md):
    m = EchoStateNetwork(n_reservoir=60, spectral_radius=0.9).fit(tiny_md)
    pred = m.predict(5)
    assert np.all(np.isfinite(pred))  # reservoir must not blow up


def test_gp_runs(tiny_md):
    m = GPReturns(max_train=100).fit(tiny_md)
    assert m.predict(1).shape == (1, tiny_md.n)
