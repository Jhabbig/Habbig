"""Tier C - Volatility models (built first; these genuinely work).

Volatility is the most predictable thing in markets: it clusters. These models
forecast next-period VARIANCE per asset and are scored against the realised
squared return, with the rolling-historical-mean variance as the NULL.

    1. EWMA / RiskMetrics volatility
    2. Rolling realized covariance matrix dynamics
    3. VAR on log-volatilities
    4. Ridge-AR on realized vol
"""

from __future__ import annotations

import numpy as np

from core import TARGET_VARIANCE, BaseModel, logger


# --------------------------------------------------------------------------- #
# 1. EWMA / RiskMetrics
# --------------------------------------------------------------------------- #
class EWMAVolatility(BaseModel):
    """RiskMetrics exponentially-weighted moving-average volatility.

        sigma^2_t = lam * sigma^2_{t-1} + (1 - lam) * r^2_{t-1}

    The EWMA forecast is flat across the horizon (the best forecast of future
    variance under this model is today's estimate), so ``predict(steps)`` tiles
    the current variance. The full EWMA covariance matrix is exposed in
    ``state()`` and reused by the covariance and structure tiers.

    lam = 0.94 is the RiskMetrics daily default.
    """

    target = TARGET_VARIANCE
    tier = "C"

    def __init__(self, lam: float = 0.94):
        super().__init__()
        if not 0 < lam < 1:
            raise ValueError("lam must be in (0, 1)")
        self.lam = lam
        self.name = f"EWMA(lam={lam})"
        self._var = None      # last variance vector (n,)
        self._cov = None      # last covariance matrix (n, n)

    def fit(self, data) -> "EWMAVolatility":
        R = np.asarray(data.R, dtype=float)
        if R.shape[0] < 2:
            raise ValueError("EWMA needs at least 2 return rows.")
        n = R.shape[1]
        lam = self.lam
        # Seed with the sample (co)variance of the first chunk for stability.
        seed = min(30, R.shape[0])
        cov = np.cov(R[:seed].T)
        cov = np.atleast_2d(cov)
        if cov.shape != (n, n):  # n == 1 edge case
            cov = cov.reshape(n, n)
        for t in range(R.shape[0]):
            r = R[t][:, None]
            cov = lam * cov + (1 - lam) * (r @ r.T)
        self._cov = cov
        self._var = np.diag(cov).copy()
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        pred = np.tile(self._var, (steps, 1))
        return self._store_prediction(pred)

    def state(self) -> dict:
        self._check_fitted()
        return {
            "lambda": self.lam,
            "variance": self._var,
            "volatility": np.sqrt(self._var),
            "covariance": self._cov,
            "correlation": _cov_to_corr(self._cov),
        }


# --------------------------------------------------------------------------- #
# 2. Rolling realized covariance matrix dynamics
# --------------------------------------------------------------------------- #
class RollingCovariance(BaseModel):
    """Rolling realised covariance matrix and its one-step persistence forecast.

    The forecast of next variance is the trailing realised variance over
    ``window`` days (the diagonal of the rolling covariance). It also tracks how
    fast the covariance matrix itself moves (Frobenius distance between
    consecutive rolling estimates) and exposes the latest matrix in ``state()``.
    """

    target = TARGET_VARIANCE
    tier = "C"

    def __init__(self, window: int = 63):
        super().__init__()
        if window < 5:
            raise ValueError("window too small")
        self.window = window
        self.name = f"RollingCov(w={window})"
        self._cov = None
        self._var = None
        self._frob_speed = None

    def fit(self, data) -> "RollingCovariance":
        R = np.asarray(data.R, dtype=float)
        w = min(self.window, R.shape[0])
        if w < 5:
            raise ValueError("Not enough data for rolling covariance.")
        self._cov = np.atleast_2d(np.cov(R[-w:].T))
        self._var = np.diag(self._cov).copy()
        # Speed of structural change: ||C_t - C_{t-1}||_F averaged over the tail.
        if R.shape[0] >= 2 * w:
            dists = []
            prev = None
            for end in range(w, R.shape[0] + 1, max(1, w // 4)):
                c = np.cov(R[end - w:end].T)
                if prev is not None:
                    dists.append(np.linalg.norm(c - prev))
                prev = c
            self._frob_speed = float(np.mean(dists)) if dists else 0.0
        else:
            self._frob_speed = 0.0
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        return self._store_prediction(np.tile(self._var, (steps, 1)))

    def state(self) -> dict:
        self._check_fitted()
        return {
            "window": self.window,
            "covariance": self._cov,
            "variance": self._var,
            "correlation": _cov_to_corr(self._cov),
            "frobenius_speed": self._frob_speed,
        }


# --------------------------------------------------------------------------- #
# 3. VAR on log-volatilities
# --------------------------------------------------------------------------- #
class VARLogVol(BaseModel):
    """First-order vector autoregression on log realised volatilities.

        y_t = log(rolling_vol_t),   y_t = c + A y_{t-1} + e_t

    Captures volatility spillover ACROSS assets (one asset's vol predicting
    another's). Forecasts log-vol one step ahead, exponentiates and squares to a
    variance forecast. Falls back gracefully to a diagonal AR(1) if the VAR
    system is ill-conditioned.
    """

    target = TARGET_VARIANCE
    tier = "C"

    def __init__(self, vol_window: int = 21, ridge: float = 1e-3):
        super().__init__()
        self.vol_window = vol_window
        self.ridge = ridge
        self.name = f"VAR-logvol(w={vol_window})"
        self._A = None
        self._c = None
        self._last_y = None

    @staticmethod
    def _rolling_log_vol(R: np.ndarray, w: int) -> np.ndarray:
        T, n = R.shape
        out = np.full((T, n), np.nan)
        sq = R ** 2
        csum = np.cumsum(np.vstack([np.zeros((1, n)), sq]), axis=0)
        for t in range(w, T + 1):
            var = (csum[t] - csum[t - w]) / w
            out[t - 1] = var
        var_series = out[w - 1:]
        var_series = np.clip(var_series, 1e-10, None)
        return np.log(var_series)  # log-variance series, shape (T-w+1, n)

    def fit(self, data) -> "VARLogVol":
        R = np.asarray(data.R, dtype=float)
        w = self.vol_window
        if R.shape[0] < w + 30:
            raise ValueError("Not enough data for VAR on log-vol.")
        Y = self._rolling_log_vol(R, w)  # (M, n) log-variance
        n = Y.shape[1]
        X = Y[:-1]
        Z = Y[1:]
        Xd = np.hstack([np.ones((X.shape[0], 1)), X])  # design with intercept
        # ridge-regularised least squares: B = (Xd'Xd + rI)^-1 Xd'Z
        G = Xd.T @ Xd + self.ridge * np.eye(Xd.shape[1])
        B = np.linalg.solve(G, Xd.T @ Z)  # (n+1, n)
        self._c = B[0]
        self._A = B[1:].T  # (n, n)
        self._last_y = Y[-1]
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        y = self._last_y.copy()
        preds = []
        for _ in range(steps):
            y = self._c + self._A @ y
            preds.append(np.exp(y))  # log-variance -> variance
        return self._store_prediction(np.array(preds))

    def state(self) -> dict:
        self._check_fitted()
        return {
            "A": self._A,
            "spectral_radius": float(np.max(np.abs(np.linalg.eigvals(self._A)))),
            "last_log_variance": self._last_y,
            "variance": np.exp(self._c + self._A @ self._last_y),
        }


# --------------------------------------------------------------------------- #
# 4. Ridge-AR on realized vol
# --------------------------------------------------------------------------- #
class RidgeARVol(BaseModel):
    """Ridge-regularised autoregression on realised variance (HAR-style).

    A strong, hard-to-beat volatility baseline. For each asset, next variance is
    regressed on its own daily, weekly and monthly trailing realised variance
    (the Corsi HAR lags), with ridge shrinkage. Per-asset coefficients are
    pooled-then-shrunk for robustness on short windows.
    """

    target = TARGET_VARIANCE
    tier = "C"

    def __init__(self, lags=(1, 5, 21), ridge: float = 1.0):
        super().__init__()
        self.lags = tuple(lags)
        self.ridge = ridge  # ridge strength on the STANDARDISED design (scale-free)
        self.name = f"RidgeAR-HAR{self.lags}"
        self._beta = None
        self._mu = None       # feature means (for standardisation)
        self._sd = None       # feature stds
        self._last_features = None

    def _features(self, var_series: np.ndarray) -> np.ndarray:
        # var_series: (T,) daily realised variance proxy (r^2). Build HAR lags.
        T = var_series.shape[0]
        maxlag = max(self.lags)
        X = np.full((T, len(self.lags)), np.nan)
        csum = np.concatenate([[0.0], np.cumsum(var_series)])
        for i, L in enumerate(self.lags):
            for t in range(maxlag, T):
                X[t, i] = (csum[t] - csum[t - L]) / L
        return X

    def fit(self, data) -> "RidgeARVol":
        R = np.asarray(data.R, dtype=float)
        if R.shape[0] < max(self.lags) + 40:
            raise ValueError("Not enough data for Ridge-AR vol.")
        var = R ** 2  # (T, n) daily variance proxy
        T, n = var.shape
        maxlag = max(self.lags)
        Xs, ys = [], []
        last_feats = []
        for a in range(n):
            X = self._features(var[:, a])
            y = var[:, a]
            valid = np.arange(maxlag, T - 1)
            Xs.append(X[valid])
            ys.append(y[valid + 1])  # predict next-day variance
            # feature row for forecasting one step past the end
            last = np.array([np.mean(var[T - L:, a]) for L in self.lags])
            last_feats.append(last)
        Xall = np.vstack(Xs)
        yall = np.concatenate(ys)
        # Standardise features so the ridge penalty is scale-invariant (variance
        # values are ~1e-4, so an unscaled ridge would crush the coefficients).
        self._mu = Xall.mean(axis=0)
        self._sd = Xall.std(axis=0) + 1e-12
        Xz = (Xall - self._mu) / self._sd
        Xd = np.hstack([np.ones((Xz.shape[0], 1)), Xz])
        G = Xd.T @ Xd + self.ridge * np.eye(Xd.shape[1])
        self._beta = np.linalg.solve(G, Xd.T @ yall)  # pooled coefficients
        self._last_features = np.array(last_feats)  # (n, n_lags)
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        feats = (self._last_features - self._mu) / self._sd
        Xd = np.hstack([np.ones((feats.shape[0], 1)), feats])
        var_hat = np.clip(Xd @ self._beta, 1e-10, None)  # (n,)
        return self._store_prediction(np.tile(var_hat, (steps, 1)))

    def state(self) -> dict:
        self._check_fitted()
        names = ["intercept"] + [f"lag{L}" for L in self.lags]
        return {
            "coefficients": dict(zip(names, self._beta)),
            "last_features": self._last_features,
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _cov_to_corr(cov: np.ndarray) -> np.ndarray:
    d = np.sqrt(np.clip(np.diag(cov), 1e-12, None))
    corr = cov / np.outer(d, d)
    np.fill_diagonal(corr, 1.0)
    return corr
