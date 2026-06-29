"""Per-model tests for Tier C volatility models.

Each test: fit on a tiny synthetic series, assert predict() shape and that
state() is populated.
"""

import numpy as np
import pytest

from core import TARGET_VARIANCE
from models.tier_c_volatility import (
    EWMAVolatility,
    RidgeARVol,
    RollingCovariance,
    VARLogVol,
)

TIER_C = [
    lambda: EWMAVolatility(0.94),
    lambda: RollingCovariance(40),
    lambda: VARLogVol(15),
    lambda: RidgeARVol(),
]


@pytest.mark.parametrize("factory", TIER_C)
def test_fit_predict_shape(factory, tiny_md):
    m = factory().fit(tiny_md)
    assert m.target == TARGET_VARIANCE
    for steps in (1, 3):
        pred = m.predict(steps)
        assert pred.shape == (steps, tiny_md.n)
        assert np.all(np.isfinite(pred))
        assert np.all(pred > 0)  # variances are positive


@pytest.mark.parametrize("factory", TIER_C)
def test_state_populated(factory, tiny_md):
    m = factory().fit(tiny_md)
    st = m.state()
    assert isinstance(st, dict) and len(st) > 0
    assert all(v is not None for v in st.values())


@pytest.mark.parametrize("factory", TIER_C)
def test_score_returns_skill(factory, tiny_md):
    m = factory().fit(tiny_md)
    m.predict(1)
    actual = tiny_md.R[-1]  # pretend last return is the realised one
    sc = m.score(actual)
    assert {"error", "null_error", "skill"} <= set(sc)


def test_ewma_covariance_psd(tiny_md):
    m = EWMAVolatility(0.94).fit(tiny_md)
    cov = m.state()["covariance"]
    eig = np.linalg.eigvalsh(cov)
    assert np.all(eig > -1e-10)  # positive semi-definite


def test_requires_fit_first():
    with pytest.raises(RuntimeError):
        EWMAVolatility().predict(1)
