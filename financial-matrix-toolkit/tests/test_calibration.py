"""Tests for probability calibration (Platt, isotonic) and reliability."""

import numpy as np

from calibration import (
    apply_calibrator,
    calibrator_from_arrays,
    calibrator_to_arrays,
    expected_calibration_error,
    fit_calibrator,
    reliability,
)
from eventmetrics import roc_auc


def _miscalibrated(seed=0, n=3000):
    rng = np.random.default_rng(seed)
    y = rng.binomial(1, 0.3, n).astype(float)
    s = np.clip(0.5 + 0.8 * (y - 0.3) + rng.normal(0, 0.2, n), 0.01, 0.99)
    return y, s


def test_platt_improves_calibration():
    y, s = _miscalibrated()
    cal = fit_calibrator(s, y, method="platt")
    p = apply_calibrator(cal, s)
    assert expected_calibration_error(y, p) < expected_calibration_error(y, s)


def test_isotonic_improves_calibration():
    y, s = _miscalibrated(seed=1)
    cal = fit_calibrator(s, y, method="isotonic")
    p = apply_calibrator(cal, s)
    assert expected_calibration_error(y, p) < expected_calibration_error(y, s)


def test_calibration_preserves_ranking_auc():
    # Platt is strictly monotone -> AUC unchanged. Isotonic is monotone but can
    # merge distinct scores into ties, which can only nudge AUC slightly.
    y, s = _miscalibrated(seed=2)
    base = roc_auc(y, s)
    p_platt = apply_calibrator(fit_calibrator(s, y, method="platt"), s)
    assert abs(roc_auc(y, p_platt) - base) < 1e-6
    p_iso = apply_calibrator(fit_calibrator(s, y, method="isotonic"), s)
    assert abs(roc_auc(y, p_iso) - base) < 0.02


def test_serialization_roundtrip():
    y, s = _miscalibrated(seed=3)
    for method in ("platt", "isotonic"):
        cal = fit_calibrator(s, y, method=method)
        d = {k: v for k, v in calibrator_to_arrays(cal).items()}
        cal2 = calibrator_from_arrays(d)
        assert np.allclose(apply_calibrator(cal, s), apply_calibrator(cal2, s))


def test_identity_for_degenerate_input():
    y = np.ones(50)                       # single class -> identity calibrator
    s = np.random.default_rng(0).uniform(0, 1, 50)
    cal = fit_calibrator(s, y, method="platt")
    assert cal["kind"] == "identity"
    assert np.allclose(apply_calibrator(cal, s), s)


def test_reliability_bins_sum():
    y, s = _miscalibrated(seed=4)
    mp, ob, cnt = reliability(y, s, n_bins=10)
    assert cnt.sum() == len(y)
