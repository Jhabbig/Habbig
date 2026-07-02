"""Tier D - Direction models (THE HONESTY TIER).

These predict the next return / its sign. The core thesis says they should lose
to the coin-flip / random-walk null, especially after transaction costs. They are
built properly (real logistic regression, VAR, Gaussian process and an echo-state
reservoir, all from numpy) precisely so that their failure is credible: it is the
models, not a strawman, that fail to beat the efficient-market floor.

    17. Logistic / softmax regression on lagged returns -> next-day direction
    18. VAR on returns
    19. Gaussian Process regression on returns
    20. Echo State Network reservoir on returns
"""

from __future__ import annotations

import numpy as np

from core import TARGET_DIRECTION, TARGET_RETURNS, BaseModel


# --------------------------------------------------------------------------- #
# 17. Logistic regression on lagged returns -> direction
# --------------------------------------------------------------------------- #
class LogisticDirection(BaseModel):
    """L2-regularised logistic regression predicting next-day direction.

    For each asset, the previous day's cross-sectional return vector is the
    feature, and the target is the sign of that asset's next return. Trained by
    gradient descent (no ML framework). Expected to hover at ~50% accuracy: the
    sign of tomorrow's return is not a function of today's returns.
    """

    target = TARGET_DIRECTION
    tier = "D"
    name = "LogisticDirection"

    def __init__(self, l2: float = 1.0, lr: float = 0.1, epochs: int = 300, seed: int = 42):
        super().__init__()
        self.l2 = l2
        self.lr = lr
        self.epochs = epochs
        self.seed = seed
        self._Wt = None       # (n_features+1, n_assets)
        self._mu = None
        self._sd = None
        self._last_x = None

    @staticmethod
    def _sigmoid(z):
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    def fit(self, data) -> "LogisticDirection":
        R = np.asarray(data.R, dtype=float)
        X = R[:-1]                       # features: yesterday's returns (T-1, n)
        Y = (R[1:] > 0).astype(float)    # target: next-day up? (T-1, n)
        self._mu = X.mean(axis=0)
        self._sd = X.std(axis=0) + 1e-12
        Xz = (X - self._mu) / self._sd
        Xd = np.hstack([np.ones((Xz.shape[0], 1)), Xz])  # (m, p)
        m, p = Xd.shape
        n = Y.shape[1]
        rng = np.random.default_rng(self.seed)
        W = rng.normal(0, 0.01, (p, n))
        reg = self.l2 * np.ones((p, 1))
        reg[0] = 0.0  # do not penalise the intercept
        for _ in range(self.epochs):
            P = self._sigmoid(Xd @ W)
            grad = Xd.T @ (P - Y) / m + reg * W
            W -= self.lr * grad
        self._Wt = W
        self._last_x = (R[-1] - self._mu) / self._sd
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        xd = np.concatenate([[1.0], self._last_x])
        p = self._sigmoid(xd @ self._Wt)
        sign = np.where(p >= 0.5, 1.0, -1.0)
        return self._store_prediction(np.tile(sign, (steps, 1)))

    def state(self) -> dict:
        self._check_fitted()
        p = self._sigmoid(np.concatenate([[1.0], self._last_x]) @ self._Wt)
        return {
            "weights": self._Wt,
            "current_up_probability": p,
            "mean_abs_weight": float(np.mean(np.abs(self._Wt[1:]))),
        }


# --------------------------------------------------------------------------- #
# 18. VAR on returns
# --------------------------------------------------------------------------- #
class VARReturns(BaseModel):
    """First-order vector autoregression on returns with ridge shrinkage.

        R_t = c + A R_{t-1} + e_t

    Captures any linear lead-lag between assets. On efficient markets A is
    essentially noise, so the one-step forecast barely differs from the
    zero-return random-walk null - shown explicitly, net of costs.
    """

    target = TARGET_RETURNS
    tier = "D"
    name = "VAR(returns)"

    def __init__(self, ridge: float = 1.0):
        super().__init__()
        self.ridge = ridge
        self._A = None
        self._c = None
        self._last = None

    def fit(self, data) -> "VARReturns":
        R = np.asarray(data.R, dtype=float)
        X = R[:-1]
        Z = R[1:]
        Xd = np.hstack([np.ones((X.shape[0], 1)), X])
        G = Xd.T @ Xd + self.ridge * np.eye(Xd.shape[1])
        B = np.linalg.solve(G, Xd.T @ Z)
        self._c = B[0]
        self._A = B[1:].T
        self._last = R[-1]
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        r = self._last.copy()
        preds = []
        for _ in range(steps):
            r = self._c + self._A @ r
            preds.append(r)
        return self._store_prediction(np.array(preds))

    def state(self) -> dict:
        self._check_fitted()
        return {
            "A": self._A,
            "spectral_radius": float(np.max(np.abs(np.linalg.eigvals(self._A)))),
            "mean_abs_offdiag": float(np.mean(np.abs(self._A - np.diag(np.diag(self._A))))),
        }


# --------------------------------------------------------------------------- #
# 19. Gaussian Process regression on returns
# --------------------------------------------------------------------------- #
class GPReturns(BaseModel):
    """Gaussian Process regression (RBF kernel) predicting next return per asset.

    Features are the previous day's own and market return. Hyperparameters are
    set by simple heuristics (median lengthscale, variance-based signal/noise);
    training is capped to the most recent ``max_train`` points for tractability.
    A flexible non-linear learner - which, on efficient markets, still cannot
    beat the random-walk null.
    """

    target = TARGET_RETURNS
    tier = "D"
    name = "GP(returns)"

    def __init__(self, max_train: int = 150, noise_frac: float = 0.5):
        super().__init__()
        self.max_train = max_train
        self.noise_frac = noise_frac
        self._models = None  # per-asset (Xtr, alpha, ell, sf2, last_x)

    @staticmethod
    def _rbf(A, B, ell):
        d2 = (
            np.sum(A**2, axis=1)[:, None]
            + np.sum(B**2, axis=1)[None, :]
            - 2 * A @ B.T
        )
        return np.exp(-0.5 * d2 / (ell**2 + 1e-12))

    def fit(self, data) -> "GPReturns":
        R = np.asarray(data.R, dtype=float)
        market = R.mean(axis=1)
        T, n = R.shape
        self._models = []
        for a in range(n):
            feat = np.column_stack([R[:-1, a], market[:-1]])  # (T-1, 2)
            y = R[1:, a]
            if feat.shape[0] > self.max_train:
                feat = feat[-self.max_train:]
                y = y[-self.max_train:]
            ymu = y.mean()
            yc = y - ymu
            # median heuristic lengthscale
            sub = feat[:: max(1, len(feat) // 50)]
            d2 = (
                np.sum(sub**2, axis=1)[:, None]
                + np.sum(sub**2, axis=1)[None, :]
                - 2 * sub @ sub.T
            )
            ell = np.sqrt(np.median(d2[d2 > 0])) + 1e-9
            sf2 = np.var(yc) + 1e-12
            sn2 = self.noise_frac * sf2
            K = sf2 * self._rbf(feat, feat, ell) + sn2 * np.eye(len(feat))
            alpha = np.linalg.solve(K, yc)
            last_x = np.array([[R[-1, a], market[-1]]])
            self._models.append((feat, alpha, ell, sf2, ymu, last_x))
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        preds = []
        for (feat, alpha, ell, sf2, ymu, last_x) in self._models:
            kstar = sf2 * self._rbf(last_x, feat, ell)  # (1, m)
            preds.append(float(kstar[0] @ alpha + ymu))
        pred = np.array(preds)
        # GP conditions on the last observation; multi-step is held flat (the
        # honest, conservative choice - it has no dynamics to iterate).
        return self._store_prediction(np.tile(pred, (steps, 1)))

    def state(self) -> dict:
        self._check_fitted()
        return {
            "n_asset_models": len(self._models),
            "lengthscales": np.array([m[2] for m in self._models]),
            "max_train": self.max_train,
        }


# --------------------------------------------------------------------------- #
# 20. Echo State Network reservoir on returns
# --------------------------------------------------------------------------- #
class EchoStateNetwork(BaseModel):
    """Echo State Network (reservoir computing) forecasting the next returns.

    A fixed random recurrent reservoir is driven by the return series; only a
    linear readout is trained (ridge regression). Reservoir computing is powerful
    for chaotic deterministic systems - and markets are not that, so this too is
    expected to fail against the random-walk null after costs.
    """

    target = TARGET_RETURNS
    tier = "D"

    def __init__(self, n_reservoir: int = 120, spectral_radius: float = 0.9,
                 sparsity: float = 0.1, ridge: float = 1.0, seed: int = 42):
        super().__init__()
        self.n_reservoir = n_reservoir
        self.spectral_radius = spectral_radius
        self.sparsity = sparsity
        self.ridge = ridge
        self.seed = seed
        self.name = f"ESN(res={n_reservoir})"
        self._Win = None
        self._W = None
        self._Wout = None
        self._state = None
        self._last_input = None

    def _build_reservoir(self, n_inputs):
        rng = np.random.default_rng(self.seed)
        Win = rng.uniform(-0.5, 0.5, (self.n_reservoir, n_inputs))
        W = rng.standard_normal((self.n_reservoir, self.n_reservoir))
        mask = rng.random((self.n_reservoir, self.n_reservoir)) > self.sparsity
        W[mask] = 0.0
        radius = np.max(np.abs(np.linalg.eigvals(W)))
        if radius > 0:
            W *= self.spectral_radius / radius
        return Win, W

    def fit(self, data) -> "EchoStateNetwork":
        R = np.asarray(data.R, dtype=float)
        T, n = R.shape
        self._Win, self._W = self._build_reservoir(n)
        states = np.zeros((T, self.n_reservoir))
        s = np.zeros(self.n_reservoir)
        for t in range(T):
            s = np.tanh(self._Win @ R[t] + self._W @ s)
            states[t] = s
        # train readout to map state_t -> return_{t+1}
        X = states[:-1]
        Y = R[1:]
        Xd = np.hstack([np.ones((X.shape[0], 1)), X])
        G = Xd.T @ Xd + self.ridge * np.eye(Xd.shape[1])
        self._Wout = np.linalg.solve(G, Xd.T @ Y)  # (res+1, n)
        self._state = s
        self._last_input = R[-1]
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        s = self._state.copy()
        x = self._last_input.copy()
        preds = []
        for _ in range(steps):
            sd = np.concatenate([[1.0], s])
            y = sd @ self._Wout
            preds.append(y)
            # feed the prediction back to advance the reservoir
            s = np.tanh(self._Win @ y + self._W @ s)
            x = y
        return self._store_prediction(np.array(preds))

    def state(self) -> dict:
        self._check_fitted()
        return {
            "n_reservoir": self.n_reservoir,
            "spectral_radius": self.spectral_radius,
            "readout_norm": float(np.linalg.norm(self._Wout)),
            "reservoir_state_norm": float(np.linalg.norm(self._state)),
        }
