"""Tier B - Regime / state models (these partially work).

Regimes (calm vs crisis) are real and persistent, so models that identify them
add value for volatility; but turning regime knowledge into a return forecast is
much harder. These models therefore mostly target VARIANCE, with the bucketed
Markov chain (direction) and DMD (returns) included honestly - and scored
against their nulls and transaction costs.

    11. Gaussian HMM for regime identification (calm/crisis)
    12. Markov-switching mean + variance
    13. Discrete Markov chain on bucketed returns
    14. Absorbing chain: drawdown -> recovery (expected time-to-recovery)
    15. Dynamic Mode Decomposition on the returns matrix
    16. Kalman filter / local-level state space on the price level
"""

from __future__ import annotations

import numpy as np

from core import (
    TARGET_DIRECTION,
    TARGET_PRICE,
    TARGET_RETURNS,
    TARGET_VARIANCE,
    BaseModel,
)


# --------------------------------------------------------------------------- #
# Gaussian HMM via Baum-Welch (scaled) - shared by #11 and #12
# --------------------------------------------------------------------------- #
def _gaussian_hmm_em(x: np.ndarray, K: int = 2, n_iter: int = 60, seed: int = 42):
    """Fit a 1-D Gaussian HMM with the Baum-Welch (EM) algorithm.

    Returns a dict with means, variances, transition matrix A, stationary start
    distribution pi, posterior state probabilities gamma (T x K), and the final
    forward-filtered state distribution (for forecasting).
    """
    x = np.asarray(x, dtype=float).ravel()
    T = x.shape[0]
    rng = np.random.default_rng(seed)
    # init: order states by variance so state 0 = calm, K-1 = crisis.
    qs = np.quantile(x, np.linspace(0.1, 0.9, K))
    mu = qs.copy()
    var = np.full(K, np.var(x) + 1e-8)
    A = np.full((K, K), 0.1 / (K - 1)) if K > 1 else np.ones((1, 1))
    np.fill_diagonal(A, 0.9)
    A /= A.sum(axis=1, keepdims=True)
    pi = np.full(K, 1.0 / K)

    def gauss(xv, m, v):
        return np.exp(-0.5 * (xv - m) ** 2 / v) / np.sqrt(2 * np.pi * v)

    ll_old = -np.inf
    gamma = np.full((T, K), 1.0 / K)
    for _ in range(n_iter):
        B = np.empty((T, K))
        for k in range(K):
            B[:, k] = gauss(x, mu[k], var[k]) + 1e-300
        # forward with scaling
        alpha = np.zeros((T, K))
        c = np.zeros(T)
        alpha[0] = pi * B[0]
        c[0] = alpha[0].sum() + 1e-300
        alpha[0] /= c[0]
        for t in range(1, T):
            alpha[t] = (alpha[t - 1] @ A) * B[t]
            c[t] = alpha[t].sum() + 1e-300
            alpha[t] /= c[t]
        # backward
        beta = np.zeros((T, K))
        beta[-1] = 1.0
        for t in range(T - 2, -1, -1):
            beta[t] = (A @ (B[t + 1] * beta[t + 1])) / c[t + 1]
        gamma = alpha * beta
        gamma /= gamma.sum(axis=1, keepdims=True) + 1e-300
        # xi (transitions)
        xi = np.zeros((K, K))
        for t in range(T - 1):
            num = (alpha[t][:, None] * A) * (B[t + 1] * beta[t + 1])[None, :]
            xi += num / (num.sum() + 1e-300)
        # M-step
        pi = gamma[0] + 1e-12
        pi /= pi.sum()
        A = xi / (xi.sum(axis=1, keepdims=True) + 1e-300)
        for k in range(K):
            w = gamma[:, k]
            sw = w.sum() + 1e-300
            mu[k] = (w @ x) / sw
            var[k] = (w @ (x - mu[k]) ** 2) / sw + 1e-8
        ll = np.sum(np.log(c))
        if abs(ll - ll_old) < 1e-5 * (abs(ll_old) + 1):
            break
        ll_old = ll

    order = np.argsort(var)  # 0 = calm (low var), last = crisis (high var)
    mu, var = mu[order], var[order]
    A = A[np.ix_(order, order)]
    gamma = gamma[:, order]
    return {
        "mu": mu,
        "var": var,
        "A": A,
        "pi": pi[order],
        "gamma": gamma,
        "filtered_last": gamma[-1],
        "loglik": ll_old,
    }


# --------------------------------------------------------------------------- #
# 11. Gaussian HMM regime identification
# --------------------------------------------------------------------------- #
class GaussianHMMRegime(BaseModel):
    """Two-state (calm/crisis) Gaussian HMM on the market return.

    Identifies the latent market regime from the cross-sectional mean return,
    then forecasts next-period variance per asset by mixing each asset's
    regime-conditional variance with the predicted next-state distribution
    (gamma_last @ A). ``state()`` exposes the regime path, transition matrix and
    per-state volatilities.
    """

    target = TARGET_VARIANCE
    tier = "B"

    def __init__(self, n_states: int = 2, n_iter: int = 60, seed: int = 42):
        super().__init__()
        self.K = n_states
        self.n_iter = n_iter
        self.seed = seed
        self.name = f"GaussianHMM(K={n_states})"
        self._hmm = None
        self._regime_var = None  # (K, n) per-asset variance by regime

    def fit(self, data) -> "GaussianHMMRegime":
        R = np.asarray(data.R, dtype=float)
        market = R.mean(axis=1)
        self._hmm = _gaussian_hmm_em(market, self.K, self.n_iter, self.seed)
        gamma = self._hmm["gamma"]  # (T, K)
        # per-asset variance conditional on each regime (posterior-weighted)
        rv = np.zeros((self.K, R.shape[1]))
        for k in range(self.K):
            w = gamma[:, k]
            sw = w.sum() + 1e-12
            mu_k = (w[:, None] * R).sum(axis=0) / sw
            rv[k] = (w[:, None] * (R - mu_k) ** 2).sum(axis=0) / sw
        self._regime_var = rv
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        A = self._hmm["A"]
        p = self._hmm["filtered_last"].copy()
        preds = []
        for _ in range(steps):
            p = p @ A
            preds.append(p @ self._regime_var)  # (n,) mixed variance
        return self._store_prediction(np.array(preds))

    def state(self) -> dict:
        self._check_fitted()
        gamma = self._hmm["gamma"]
        current = int(np.argmax(gamma[-1]))
        return {
            "n_states": self.K,
            "transition_matrix": self._hmm["A"],
            "state_volatility_market": np.sqrt(self._hmm["var"]),
            "current_regime": current,
            "current_regime_prob": float(gamma[-1, current]),
            "regime_path": np.argmax(gamma, axis=1),
            "crisis_fraction": float(np.mean(np.argmax(gamma, axis=1) == self.K - 1)),
            "persistence": float(np.mean(np.diag(self._hmm["A"]))),
        }


# --------------------------------------------------------------------------- #
# 12. Markov-switching mean + variance
# --------------------------------------------------------------------------- #
class MarkovSwitchingMeanVar(BaseModel):
    """Two-state Markov-switching model of the market mean AND variance.

    Differs from the HMM regime model: it forecasts the market's next-period mean
    and variance, then scales each asset's unconditional variance by the predicted
    market-variance multiplier (a regime-driven vol scaler). Exposes the switching
    means, variances and the next-period regime forecast.
    """

    target = TARGET_VARIANCE
    tier = "B"

    def __init__(self, n_iter: int = 60, seed: int = 42):
        super().__init__()
        self.name = "MarkovSwitch(mean+var)"
        self.n_iter = n_iter
        self.seed = seed
        self._hmm = None
        self._uncond_var = None
        self._mean_market_var = None

    def fit(self, data) -> "MarkovSwitchingMeanVar":
        R = np.asarray(data.R, dtype=float)
        market = R.mean(axis=1)
        self._hmm = _gaussian_hmm_em(market, 2, self.n_iter, self.seed)
        self._uncond_var = R.var(axis=0)
        # expected market variance under the posterior (baseline for scaling)
        gamma = self._hmm["gamma"]
        self._mean_market_var = float(gamma.mean(axis=0) @ self._hmm["var"])
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        A = self._hmm["A"]
        p = self._hmm["filtered_last"].copy()
        preds = []
        for _ in range(steps):
            p = p @ A
            mkt_var = p @ self._hmm["var"]
            scaler = mkt_var / (self._mean_market_var + 1e-12)
            preds.append(self._uncond_var * scaler)
        return self._store_prediction(np.array(preds))

    def state(self) -> dict:
        self._check_fitted()
        gamma = self._hmm["gamma"]
        p_next = self._hmm["filtered_last"] @ self._hmm["A"]
        return {
            "state_means": self._hmm["mu"],
            "state_variances": self._hmm["var"],
            "transition_matrix": self._hmm["A"],
            "current_regime": int(np.argmax(gamma[-1])),
            "next_regime_prob_crisis": float(p_next[-1]),
            "expected_next_market_var": float(p_next @ self._hmm["var"]),
        }


# --------------------------------------------------------------------------- #
# 13. Discrete Markov chain on bucketed returns
# --------------------------------------------------------------------------- #
class MarkovChainBuckets(BaseModel):
    """Discrete Markov chain over bucketed returns (down-big/down/flat/up/up-big).

    Per asset, returns are bucketed by training quantiles, a Laplace-smoothed
    transition matrix is estimated, and the next-day direction is the sign of the
    expected next return implied by the transition row. On efficient markets the
    sign edge is tiny - the harness scores it against the coin-flip null and net
    of costs, and it is expected to (honestly) struggle.
    """

    target = TARGET_DIRECTION
    tier = "B"
    name = "MarkovChain(5-bucket)"

    def __init__(self, n_buckets: int = 5, smoothing: float = 1.0):
        super().__init__()
        self.n_buckets = n_buckets
        self.smoothing = smoothing
        self._edges = None       # (n, n_buckets-1)
        self._trans = None       # (n, B, B)
        self._bucket_mean = None  # (n, B)
        self._last_bucket = None  # (n,)

    def _bucketize(self, col, edges):
        return np.clip(np.searchsorted(edges, col), 0, self.n_buckets - 1)

    def fit(self, data) -> "MarkovChainBuckets":
        R = np.asarray(data.R, dtype=float)
        T, n = R.shape
        B = self.n_buckets
        self._edges = np.zeros((n, B - 1))
        self._trans = np.zeros((n, B, B))
        self._bucket_mean = np.zeros((n, B))
        self._last_bucket = np.zeros(n, dtype=int)
        qs = np.linspace(0, 1, B + 1)[1:-1]
        for a in range(n):
            edges = np.quantile(R[:, a], qs)
            self._edges[a] = edges
            b = self._bucketize(R[:, a], edges)
            # transition counts with Laplace smoothing
            M = np.full((B, B), self.smoothing)
            for t in range(T - 1):
                M[b[t], b[t + 1]] += 1
            M /= M.sum(axis=1, keepdims=True)
            self._trans[a] = M
            for k in range(B):
                vals = R[b == k, a]
                self._bucket_mean[a, k] = vals.mean() if len(vals) else 0.0
            self._last_bucket[a] = b[-1]
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        n = self._trans.shape[0]
        preds = []
        state = [np.eye(self.n_buckets)[self._last_bucket[a]] for a in range(n)]
        for _ in range(steps):
            row = np.zeros(n)
            for a in range(n):
                state[a] = state[a] @ self._trans[a]
                exp_ret = state[a] @ self._bucket_mean[a]
                row[a] = np.sign(exp_ret) if exp_ret != 0 else 1.0
            preds.append(row)
        return self._store_prediction(np.array(preds))

    def state(self) -> dict:
        self._check_fitted()
        # stationary distribution of the first asset's chain (illustrative)
        M = self._trans[0]
        evals, evecs = np.linalg.eig(M.T)
        stat = np.real(evecs[:, np.argmin(np.abs(evals - 1))])
        stat = stat / stat.sum()
        return {
            "n_buckets": self.n_buckets,
            "last_bucket": self._last_bucket,
            "transition_example": M,
            "stationary_example": stat,
            "bucket_means": self._bucket_mean,
        }


# --------------------------------------------------------------------------- #
# 14. Absorbing chain: drawdown -> recovery
# --------------------------------------------------------------------------- #
class AbsorbingDrawdownChain(BaseModel):
    """Absorbing Markov chain over drawdown-depth states.

    The market index (mean log-price) is converted to a drawdown series; depths
    are bucketed into transient states, with "fully recovered" (0 drawdown) as
    the single ABSORBING state. The fundamental matrix N = (I - Q)^{-1} gives the
    expected time-to-recovery from each drawdown depth.

    A diagnostic, not a forecaster: ``predict()`` raises; use
    ``expected_time_to_recovery()`` and ``state()``.
    """

    target = None
    tier = "B"
    name = "AbsorbingChain(recovery)"

    def __init__(self, depth_edges=(0.0, 0.02, 0.05, 0.10, 0.20)):
        super().__init__()
        self.depth_edges = np.array(depth_edges)
        self._N = None
        self._expected_steps = None
        self._current_state = None

    def _drawdown_states(self, P):
        idx = np.exp(np.cumsum(np.diff(np.log(P), axis=0).mean(axis=1)))
        idx = np.concatenate([[1.0], idx])
        running_max = np.maximum.accumulate(idx)
        dd = 1.0 - idx / running_max  # drawdown depth in [0,1)
        # state 0 = recovered (dd ~ 0, absorbing); 1..m = increasing depth
        states = np.searchsorted(self.depth_edges, dd, side="right") - 1
        states = np.clip(states, 0, len(self.depth_edges) - 1)
        return states, dd

    def fit(self, data) -> "AbsorbingDrawdownChain":
        P = np.asarray(data.P, dtype=float)
        states, dd = self._drawdown_states(P)
        m = len(self.depth_edges)  # number of states; 0 = recovered (absorbing)
        counts = np.zeros((m, m))
        for t in range(len(states) - 1):
            counts[states[t], states[t + 1]] += 1
        # transient states are 1..m-1; absorbing is 0
        transient = list(range(1, m))
        Q = np.zeros((len(transient), len(transient)))
        row_tot = counts.sum(axis=1)
        for i, si in enumerate(transient):
            if row_tot[si] > 0:
                for j, sj in enumerate(transient):
                    Q[i, j] = counts[si, sj] / row_tot[si]
        try:
            N = np.linalg.inv(np.eye(len(transient)) - Q)
            expected_steps = N.sum(axis=1)  # expected days to recovery from each depth
        except np.linalg.LinAlgError:
            N = np.full((len(transient), len(transient)), np.nan)
            expected_steps = np.full(len(transient), np.nan)
        self._N = N
        self._expected_steps = expected_steps
        self._transient = transient
        self._current_state = int(states[-1])
        self._current_dd = float(dd[-1])
        self._data = data
        self._fitted = True
        return self

    def expected_time_to_recovery(self) -> float:
        """Expected days to full recovery from the CURRENT drawdown depth."""
        self._check_fitted()
        if self._current_state == 0:
            return 0.0
        i = self._transient.index(self._current_state)
        return float(self._expected_steps[i])

    def state(self) -> dict:
        self._check_fitted()
        labels = [f"({self.depth_edges[s-1]:.0%},{self.depth_edges[s]:.0%}]"
                  if s < len(self.depth_edges) - 1 else f">{self.depth_edges[s]:.0%}"
                  for s in self._transient]
        return {
            "current_drawdown": self._current_dd,
            "current_state": self._current_state,
            "expected_recovery_days_by_depth": {k: round(float(v), 1)
                                                for k, v in zip(labels, self._expected_steps)},
            "expected_recovery_days_now": self.expected_time_to_recovery(),
            "fundamental_matrix": self._N,
        }


# --------------------------------------------------------------------------- #
# 15. Dynamic Mode Decomposition on the returns matrix
# --------------------------------------------------------------------------- #
class DMDReturns(BaseModel):
    """Dynamic Mode Decomposition: fit a low-rank linear operator A with
    X' ~ A X on the returns panel and use it to forecast the next return vector.

    DMD finds spatiotemporal modes (eigenvalues encode growth/decay and
    oscillation). Truncating the SVD regularises the operator. On efficient
    markets the one-step return forecast is expected to barely beat the
    random-walk (zero-return) null - reported honestly with transaction costs.
    """

    target = TARGET_RETURNS
    tier = "B"

    def __init__(self, rank: int = 5):
        super().__init__()
        self.rank = rank
        self.name = f"DMD(r={rank})"
        self._A = None
        self._last = None
        self._eigs = None

    def fit(self, data) -> "DMDReturns":
        R = np.asarray(data.R, dtype=float)
        X = R[:-1].T   # (n, T-1)
        Xp = R[1:].T   # (n, T-1)
        r = min(self.rank, min(X.shape))
        U, S, Vt = np.linalg.svd(X, full_matrices=False)
        U_r, S_r, V_r = U[:, :r], S[:r], Vt[:r].T
        Sinv = np.diag(1.0 / np.clip(S_r, 1e-12, None))
        Atil = U_r.T @ Xp @ V_r @ Sinv          # reduced operator (r x r)
        self._eigs = np.linalg.eigvals(Atil)
        self._A = U_r @ Atil @ U_r.T            # full-state operator (n x n)
        self._last = R[-1]
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        x = self._last.copy()
        preds = []
        for _ in range(steps):
            x = np.real(self._A @ x)
            preds.append(x)
        return self._store_prediction(np.array(preds))

    def state(self) -> dict:
        self._check_fitted()
        return {
            "rank": self.rank,
            "dmd_eigenvalues": self._eigs,
            "spectral_radius": float(np.max(np.abs(self._eigs))),
            "n_growing_modes": int(np.sum(np.abs(self._eigs) > 1.0)),
        }


# --------------------------------------------------------------------------- #
# 16. Kalman filter / local-level state space on the price level
# --------------------------------------------------------------------------- #
class KalmanLocalLevel(BaseModel):
    """Local-level (random-walk-plus-noise) Kalman filter on the price level.

        level_t = level_{t-1} + w_t,   price_t = level_t + v_t

    The canonical state space for a price. Its one-step forecast is the filtered
    level - which, for a near-random-walk price, is essentially today's price.
    Scored against the random-walk null, it is expected to show ~zero skill: a
    feature, not a bug, and a clean demonstration that price LEVELS are a random
    walk. As the signal-to-noise ratio rises the filter converges exactly to the
    random walk (skill -> 0); any smoothing costs a little, so it never beats it.
    ``state()`` exposes the estimated noise variances per asset.
    """

    target = TARGET_PRICE
    tier = "B"
    name = "KalmanLocalLevel"

    def __init__(self, q_over_r: float = 10.0):
        super().__init__()
        self.q_over_r = q_over_r  # signal-to-noise ratio (state/obs variance)
        self._level = None
        self._P = None
        self._Q = None
        self._R = None

    def fit(self, data) -> "KalmanLocalLevel":
        P = np.asarray(data.P, dtype=float)
        T, n = P.shape
        # scale noise to each asset's observation variability
        obs_var = np.var(np.diff(P, axis=0), axis=0) + 1e-12
        self._R = obs_var
        self._Q = self.q_over_r * obs_var
        level = P[0].copy()
        Pcov = obs_var.copy()
        for t in range(1, T):
            # predict
            Pcov = Pcov + self._Q
            # update
            K = Pcov / (Pcov + self._R)
            level = level + K * (P[t] - level)
            Pcov = (1 - K) * Pcov
        self._level = level
        self._P = Pcov
        self._data = data
        self._fitted = True
        return self

    def predict(self, steps: int = 1) -> np.ndarray:
        self._check_fitted()
        # local level: forecast is flat at the current filtered level
        return self._store_prediction(np.tile(self._level, (steps, 1)))

    def state(self) -> dict:
        self._check_fitted()
        return {
            "filtered_level": self._level,
            "state_covariance": self._P,
            "obs_noise_var": self._R,
            "state_noise_var": self._Q,
            "signal_to_noise": self.q_over_r,
        }
