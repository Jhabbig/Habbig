"""core.py - Shared interface, metrics, and NULL benchmarks for the toolkit.

The single most important idea in this toolkit (see README / CORE THESIS):

    Return DIRECTION is near-unpredictable. The predictable structure is
    cross-asset correlation, market regime, and volatility magnitude.

Every model is judged against a NULL benchmark. A model that does not beat its
null has no skill, no matter how sophisticated it looks. The nulls live here so
that no model can quietly grade its own homework.

Conventions
-----------
* ``P``  : price matrix, shape (T, n)   -- T dates, n assets.
* ``R``  : log-return matrix, shape (T-1, n).
* Volatility models forecast next-period VARIANCE (sigma^2) per asset; they are
  scored against the realised squared return r^2 (an unbiased variance proxy).
* Direction models forecast the SIGN of the next return (+1 / -1).
* Price models forecast the next price LEVEL.
"""

from __future__ import annotations

import logging
from abc import ABC
from typing import Any

import numpy as np

SEED = 42

logger = logging.getLogger("fmt")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)


def set_global_seed(seed: int = SEED) -> None:
    """Seed numpy's legacy global RNG. Models that need their own stream should
    use ``np.random.default_rng(SEED)`` for reproducibility."""
    np.random.seed(seed)


# Target kinds a model can forecast. Decomposition models use ``None``.
TARGET_VARIANCE = "variance"   # next-period sigma^2 per asset
TARGET_DIRECTION = "direction"  # sign of next return
TARGET_PRICE = "price"          # next price level
TARGET_RETURNS = "returns"      # next return value
VALID_TARGETS = {TARGET_VARIANCE, TARGET_DIRECTION, TARGET_PRICE, TARGET_RETURNS, None}


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def rmse(forecast: np.ndarray, actual: np.ndarray) -> float:
    forecast, actual = np.broadcast_arrays(
        np.asarray(forecast, dtype=float), np.asarray(actual, dtype=float)
    )
    mask = np.isfinite(forecast) & np.isfinite(actual)
    if not mask.any():
        return float("nan")
    return float(np.sqrt(np.mean((forecast[mask] - actual[mask]) ** 2)))


def mae(forecast: np.ndarray, actual: np.ndarray) -> float:
    forecast, actual = np.broadcast_arrays(
        np.asarray(forecast, dtype=float), np.asarray(actual, dtype=float)
    )
    mask = np.isfinite(forecast) & np.isfinite(actual)
    if not mask.any():
        return float("nan")
    return float(np.mean(np.abs(forecast[mask] - actual[mask])))


def qlike(var_forecast: np.ndarray, realized_var: np.ndarray, eps: float = 1e-12) -> float:
    """QLIKE loss for variance forecasts - robust to the noisy r^2 proxy.

    QLIKE = mean( realized/forecast - log(realized/forecast) - 1 ).
    Lower is better; it is minimised in expectation by the true variance.
    """
    f, a = np.broadcast_arrays(
        np.asarray(var_forecast, dtype=float), np.asarray(realized_var, dtype=float)
    )
    mask = np.isfinite(f) & np.isfinite(a) & (f > eps)
    if not mask.any():
        return float("nan")
    ratio = np.clip(a[mask], 0, None) / f[mask]
    ratio = np.clip(ratio, eps, None)
    return float(np.mean(ratio - np.log(ratio) - 1.0))


def directional_accuracy(pred_sign: np.ndarray, actual_return: np.ndarray) -> float:
    """Fraction of correct sign predictions. Zero actual returns are dropped."""
    pred_sign = np.sign(np.asarray(pred_sign, dtype=float))
    actual_sign = np.sign(np.asarray(actual_return, dtype=float))
    mask = (actual_sign != 0) & np.isfinite(pred_sign) & np.isfinite(actual_sign)
    if not mask.any():
        return float("nan")
    return float(np.mean(pred_sign[mask] == actual_sign[mask]))


def skill(model_error: float, null_error: float) -> float:
    """Percent improvement over the null. Positive => model beats the null.

    skill = (null_error - model_error) / null_error.
    """
    if null_error is None or not np.isfinite(null_error) or null_error == 0:
        return float("nan")
    return float((null_error - model_error) / null_error)


# --------------------------------------------------------------------------- #
# NULL benchmarks -- the bar every model must clear.
# --------------------------------------------------------------------------- #
def null_random_walk_price(price_history: np.ndarray, steps: int) -> np.ndarray:
    """Random-walk null for PRICE level: tomorrow == today.

    Returns an array of shape (steps, n) repeating the last observed price.
    """
    last = np.asarray(price_history, dtype=float)[-1]
    return np.tile(last, (steps, 1))


def null_zero_return(n_assets: int, steps: int) -> np.ndarray:
    """Random-walk null for RETURNS: best guess of the next log-return is 0."""
    return np.zeros((steps, n_assets), dtype=float)


def null_base_rate_direction(return_history: np.ndarray, steps: int) -> np.ndarray:
    """Base-rate (coin-flip) null for DIRECTION.

    Predicts the historically most common sign per asset. With near-symmetric
    returns this is essentially a coin flip - which is exactly the point.
    Returns shape (steps, n) of +1/-1.
    """
    r = np.asarray(return_history, dtype=float)
    majority = np.where(np.nansum(np.sign(r), axis=0) >= 0, 1.0, -1.0)
    return np.tile(majority, (steps, 1))


def null_rolling_mean_variance(return_history: np.ndarray, window: int, steps: int) -> np.ndarray:
    """Rolling-historical-mean null for VARIANCE.

    Forecasts next variance as the mean squared return over the trailing
    ``window``. Returns shape (steps, n).
    """
    r = np.asarray(return_history, dtype=float)
    w = min(window, r.shape[0])
    var = np.nanmean(r[-w:] ** 2, axis=0)
    return np.tile(var, (steps, 1))


# --------------------------------------------------------------------------- #
# Base model
# --------------------------------------------------------------------------- #
class BaseModel(ABC):
    """Common interface for every model in the toolkit.

        fit(self, data)      -> self
        predict(self, steps) -> np.ndarray
        state(self)          -> dict   (current regime / matrix / vol estimate)
        score(self, actual)  -> dict   (error + improvement-over-null)

    Decomposition models (PCA/SVD/NMF/RMT) set ``target = None``, raise
    NotImplementedError from predict(), and expose transform()/reconstruct()
    instead. They never fake a forecast.
    """

    #: One of VALID_TARGETS. Drives null selection in the harness.
    target: str | None = None
    #: Human-readable name and tier, set by subclasses.
    name: str = "BaseModel"
    tier: str = "?"
    #: Number of trailing observations used to build the variance null.
    null_window: int = 63

    def __init__(self) -> None:
        self._fitted = False
        self._data = None
        self._last_prediction: np.ndarray | None = None

    # -- core API --------------------------------------------------------- #
    def fit(self, data: Any) -> "BaseModel":  # pragma: no cover - overridden
        raise NotImplementedError

    def predict(self, steps: int = 1) -> np.ndarray:  # pragma: no cover - overridden
        raise NotImplementedError(
            f"{self.name} is a decomposition model: use transform()/reconstruct(), not predict()."
        )

    def state(self) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- shared scoring --------------------------------------------------- #
    def _check_fitted(self) -> None:
        if not self._fitted:
            raise RuntimeError(f"{self.name}: call fit() before this method.")

    def score(self, actual: np.ndarray) -> dict:
        """Single-shot score of the most recent ``predict()`` against ``actual``.

        Computes the model error, the appropriate NULL error, and the SKILL
        (% improvement over the null). The authoritative out-of-sample number
        comes from ``harness.walk_forward``; this is a convenience for the
        single-window case and the tests.
        """
        self._check_fitted()
        if self.target is None:
            raise NotImplementedError(f"{self.name} is a decomposition model and has no score().")
        if self._last_prediction is None:
            raise RuntimeError(f"{self.name}: call predict() before score().")

        actual = np.asarray(actual, dtype=float)
        forecast = np.asarray(self._last_prediction, dtype=float)
        steps = forecast.shape[0] if forecast.ndim == 2 else 1
        hist_R = self._data.R if self._data is not None else None

        if self.target == TARGET_VARIANCE:
            realized_var = actual ** 2
            model_err = rmse(forecast, realized_var)
            null = null_rolling_mean_variance(hist_R, self.null_window, steps)
            null_err = rmse(null, realized_var)
            return {
                "error": model_err,
                "null_error": null_err,
                "skill": skill(model_err, null_err),
                "qlike": qlike(forecast, realized_var),
                "metric": "RMSE(variance)",
            }

        if self.target in (TARGET_RETURNS,):
            model_err = rmse(forecast, actual)
            null = null_zero_return(actual.shape[-1], steps)
            null_err = rmse(null, actual)
            return {
                "error": model_err,
                "null_error": null_err,
                "skill": skill(model_err, null_err),
                "metric": "RMSE(returns)",
            }

        if self.target == TARGET_PRICE:
            model_err = rmse(forecast, actual)
            null = null_random_walk_price(self._data.P, steps)
            null_err = rmse(null, actual)
            return {
                "error": model_err,
                "null_error": null_err,
                "skill": skill(model_err, null_err),
                "metric": "RMSE(price)",
            }

        if self.target == TARGET_DIRECTION:
            acc = directional_accuracy(forecast, actual)
            null = null_base_rate_direction(hist_R, steps)
            null_acc = directional_accuracy(null, actual)
            # For direction, "error" = 1 - accuracy so that lower is better and
            # skill keeps its "improvement" meaning.
            model_err = 1.0 - acc
            null_err = 1.0 - null_acc
            return {
                "error": model_err,
                "accuracy": acc,
                "null_error": null_err,
                "null_accuracy": null_acc,
                "skill": skill(model_err, null_err),
                "metric": "1-accuracy",
            }

        raise ValueError(f"Unknown target {self.target!r}")

    # -- helpers ---------------------------------------------------------- #
    def _store_prediction(self, pred: np.ndarray) -> np.ndarray:
        pred = np.atleast_2d(np.asarray(pred, dtype=float))
        self._last_prediction = pred
        return pred
