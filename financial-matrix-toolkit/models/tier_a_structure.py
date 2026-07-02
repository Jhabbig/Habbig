"""Tier A - Structure / cross-asset models (these work).

The predictable structure in markets is cross-asset: which assets move together,
how many independent factors drive the market, and when that structure breaks.
Every model here is a DECOMPOSITION or DETECTOR: it does not forecast returns, so
``predict()`` raises NotImplementedError and the model exposes
``transform()`` / ``reconstruct()`` (or ``detect()``) instead. Never fake a forecast.

    5.  PCA on returns (eigen-portfolios)
    6.  Random Matrix Theory filter (Marchenko-Pastur)
    7.  Truncated SVD of returns
    8.  Rolling correlation + Frobenius change detector
    9.  Graph Laplacian over the asset network
    10. NMF on absolute returns (co-movement clusters)
"""

from __future__ import annotations

import numpy as np

from core import BaseModel, logger


def _corr(R: np.ndarray) -> np.ndarray:
    C = np.corrcoef(R.T)
    return np.atleast_2d(C)


# --------------------------------------------------------------------------- #
# 5. PCA on returns -> eigen-portfolios
# --------------------------------------------------------------------------- #
class PCAReturns(BaseModel):
    """Principal component analysis of returns: eigen-portfolios and how many
    factors explain the market.

    The first eigen-portfolio is typically the "market" (all assets loading with
    the same sign). ``state()`` reports the explained-variance spectrum and the
    number of factors needed to reach a variance threshold.
    """

    target = None
    tier = "A"
    name = "PCA(returns)"

    def __init__(self, threshold: float = 0.90, standardize: bool = True):
        super().__init__()
        self.threshold = threshold
        self.standardize = standardize
        self._evals = None
        self._evecs = None
        self._mean = None
        self._scale = None

    def fit(self, data) -> "PCAReturns":
        R = np.asarray(data.R, dtype=float)
        self._mean = R.mean(axis=0)
        self._scale = R.std(axis=0) + 1e-12 if self.standardize else np.ones(R.shape[1])
        Z = (R - self._mean) / self._scale
        C = np.cov(Z.T)
        evals, evecs = np.linalg.eigh(np.atleast_2d(C))
        order = np.argsort(evals)[::-1]
        self._evals = np.clip(evals[order], 0, None)
        self._evecs = evecs[:, order]  # columns are eigen-portfolios
        self._data = data
        self._fitted = True
        return self

    def transform(self, data=None, k: int | None = None) -> np.ndarray:
        """Project returns onto the top-k eigen-portfolios -> factor scores (T, k)."""
        self._check_fitted()
        R = np.asarray((data or self._data).R, dtype=float)
        Z = (R - self._mean) / self._scale
        k = k or self.n_factors()
        return Z @ self._evecs[:, :k]

    def reconstruct(self, data=None, k: int | None = None) -> np.ndarray:
        """Reconstruct returns from the top-k factors (de-noised returns)."""
        self._check_fitted()
        R = np.asarray((data or self._data).R, dtype=float)
        Z = (R - self._mean) / self._scale
        k = k or self.n_factors()
        Zk = Z @ self._evecs[:, :k] @ self._evecs[:, :k].T
        return Zk * self._scale + self._mean

    def explained_variance_ratio(self) -> np.ndarray:
        return self._evals / self._evals.sum()

    def n_factors(self) -> int:
        cum = np.cumsum(self.explained_variance_ratio())
        return int(np.searchsorted(cum, self.threshold) + 1)

    def state(self) -> dict:
        self._check_fitted()
        evr = self.explained_variance_ratio()
        return {
            "eigenvalues": self._evals,
            "explained_variance_ratio": evr,
            "cumulative_variance": np.cumsum(evr),
            "market_factor_share": float(evr[0]),
            f"n_factors_{int(self.threshold*100)}pct": self.n_factors(),
            "market_eigenportfolio": self._evecs[:, 0],
        }


# --------------------------------------------------------------------------- #
# 6. Random Matrix Theory filter (Marchenko-Pastur)
# --------------------------------------------------------------------------- #
class RMTFilter(BaseModel):
    """Marchenko-Pastur filter: separate signal eigenvalues from noise and clean
    the correlation matrix.

    For a correlation matrix estimated from T samples of n assets, pure-noise
    eigenvalues fall below lambda_+ = (1 + sqrt(n/T))^2. Eigenvalues above
    lambda_+ carry signal. The cleaned matrix keeps the signal eigen-pairs and
    replaces the bulk with their average (preserving the trace), yielding a much
    more stable correlation/covariance estimate for portfolio work.
    """

    target = None
    tier = "A"
    name = "RMT-filter(MP)"

    def __init__(self):
        super().__init__()
        self._C = None
        self._C_clean = None
        self._evals = None
        self._evecs = None
        self.lambda_plus = None
        self.lambda_minus = None
        self.n_signal = None

    def fit(self, data) -> "RMTFilter":
        R = np.asarray(data.R, dtype=float)
        T, n = R.shape
        if T <= n:
            logger.warning("RMT: T (%d) <= n (%d); MP bounds are unreliable.", T, n)
        q = n / T
        self.lambda_plus = (1 + np.sqrt(q)) ** 2
        self.lambda_minus = (1 - np.sqrt(q)) ** 2
        C = _corr(R)
        evals, evecs = np.linalg.eigh(C)
        order = np.argsort(evals)[::-1]
        evals, evecs = evals[order], evecs[:, order]
        self._evals, self._evecs = evals, evecs

        signal_mask = evals > self.lambda_plus
        self.n_signal = int(signal_mask.sum())
        # Replace the noise bulk with its average eigenvalue (trace preserved).
        cleaned_evals = evals.copy()
        noise = ~signal_mask
        if noise.any():
            cleaned_evals[noise] = evals[noise].mean()
        C_clean = (evecs * cleaned_evals) @ evecs.T
        # Renormalise to unit diagonal (a correlation matrix).
        d = np.sqrt(np.clip(np.diag(C_clean), 1e-12, None))
        C_clean = C_clean / np.outer(d, d)
        np.fill_diagonal(C_clean, 1.0)
        self._C, self._C_clean = C, C_clean
        self._data = data
        self._fitted = True
        return self

    def transform(self, data=None) -> np.ndarray:
        """Return the cleaned correlation matrix."""
        self._check_fitted()
        return self._C_clean

    def reconstruct(self, data=None) -> np.ndarray:
        """Alias for the cleaned correlation matrix (the de-noised structure)."""
        return self.transform(data)

    def state(self) -> dict:
        self._check_fitted()
        return {
            "lambda_plus": float(self.lambda_plus),
            "lambda_minus": float(self.lambda_minus),
            "n_signal_eigenvalues": self.n_signal,
            "noise_fraction": float(1 - self.n_signal / len(self._evals)),
            "eigenvalues": self._evals,
            "raw_correlation": self._C,
            "cleaned_correlation": self._C_clean,
        }


# --------------------------------------------------------------------------- #
# 7. Truncated SVD of returns
# --------------------------------------------------------------------------- #
class TruncatedSVDReturns(BaseModel):
    """Truncated SVD of the (demeaned) returns matrix R = U S V^T.

    A low-rank view of the return panel: the top singular triplets capture the
    dominant common moves; truncating to rank k de-noises the panel.
    """

    target = None
    tier = "A"
    name = "TruncatedSVD"

    def __init__(self, threshold: float = 0.90):
        super().__init__()
        self.threshold = threshold
        self._U = None
        self._S = None
        self._Vt = None
        self._mean = None

    def fit(self, data) -> "TruncatedSVDReturns":
        R = np.asarray(data.R, dtype=float)
        self._mean = R.mean(axis=0)
        X = R - self._mean
        self._U, self._S, self._Vt = np.linalg.svd(X, full_matrices=False)
        self._data = data
        self._fitted = True
        return self

    def rank(self) -> int:
        ev = self._S ** 2
        cum = np.cumsum(ev) / ev.sum()
        return int(np.searchsorted(cum, self.threshold) + 1)

    def transform(self, k: int | None = None) -> np.ndarray:
        """Factor scores: U[:, :k] * S[:k], shape (T, k)."""
        self._check_fitted()
        k = k or self.rank()
        return self._U[:, :k] * self._S[:k]

    def reconstruct(self, k: int | None = None) -> np.ndarray:
        """Rank-k reconstruction of the returns matrix."""
        self._check_fitted()
        k = k or self.rank()
        Xk = (self._U[:, :k] * self._S[:k]) @ self._Vt[:k]
        return Xk + self._mean

    def state(self) -> dict:
        self._check_fitted()
        ev = self._S ** 2
        return {
            "singular_values": self._S,
            "explained_variance_ratio": ev / ev.sum(),
            f"rank_{int(self.threshold*100)}pct": self.rank(),
            "right_vectors": self._Vt,
        }


# --------------------------------------------------------------------------- #
# 8. Rolling correlation + Frobenius change detector
# --------------------------------------------------------------------------- #
class CorrelationChangeDetector(BaseModel):
    """Detect structural breaks in the correlation matrix.

    Computes a rolling correlation matrix and the Frobenius distance between
    consecutive estimates. Spikes in that distance are the "the matrix changed"
    signal - regime shifts where cross-asset structure reorganises (e.g. the
    calm -> crisis transition). Breaks are flagged where the distance exceeds
    ``z_threshold`` robust standard deviations above its median.

    This is a detector, not a forecaster: use ``detect()``; ``predict()`` raises.
    """

    target = None
    tier = "A"
    name = "CorrChangeDetector"

    def __init__(self, window: int = 63, z_threshold: float = 3.0):
        super().__init__()
        self.window = window
        self.z_threshold = z_threshold
        self._dist = None
        self._dates = None
        self._breaks = None

    def fit(self, data) -> "CorrelationChangeDetector":
        R = np.asarray(data.R, dtype=float)
        w = self.window
        T = R.shape[0]
        if T < 2 * w:
            raise ValueError("Not enough data for the correlation change detector.")
        dists = []
        idx = []
        prev = None
        for end in range(w, T + 1):
            C = _corr(R[end - w:end])
            if prev is not None:
                dists.append(float(np.linalg.norm(C - prev)))
                idx.append(end)
            prev = C
        self._dist = np.array(dists)
        self._idx = np.array(idx)
        # map to dates (price dates index by R-row+1)
        self._dates = data.dates[np.clip(self._idx, 0, len(data.dates) - 1)]
        med = np.median(self._dist)
        mad = np.median(np.abs(self._dist - med)) + 1e-12
        robust_z = 0.6745 * (self._dist - med) / mad
        self._z = robust_z
        self._breaks = np.where(robust_z > self.z_threshold)[0]
        # Merge consecutive flagged windows into distinct EVENTS: a single regime
        # shift spans many overlapping windows, so contiguous flags are one event.
        self._events = []
        if len(self._breaks):
            run_start = self._breaks[0]
            for a, b in zip(self._breaks[:-1], self._breaks[1:]):
                if b - a > self.window // 2:
                    self._events.append(run_start)
                    run_start = b
            self._events.append(run_start)
        self._events = np.array(self._events, dtype=int)
        self._data = data
        self._fitted = True
        return self

    def detect(self) -> dict:
        """Return the break locations and the distance signal."""
        self._check_fitted()
        return {
            "break_indices": self._idx[self._breaks] if len(self._breaks) else np.array([], dtype=int),
            "break_dates": [self._dates[b] for b in self._breaks],
            "event_indices": self._idx[self._events] if len(self._events) else np.array([], dtype=int),
            "event_dates": [self._dates[b] for b in self._events],
            "distance_series": self._dist,
            "robust_z": self._z,
        }

    def state(self) -> dict:
        self._check_fitted()
        return {
            "window": self.window,
            "z_threshold": self.z_threshold,
            "n_breaks": int(len(self._breaks)),
            "n_events": int(len(self._events)),
            "last_distance": float(self._dist[-1]),
            "last_z": float(self._z[-1]),
            "is_break_now": bool(self._z[-1] > self.z_threshold),
            "max_distance": float(self._dist.max()),
        }


# --------------------------------------------------------------------------- #
# 9. Graph Laplacian over the asset network
# --------------------------------------------------------------------------- #
class GraphLaplacian(BaseModel):
    """Spectral analysis of the asset correlation network.

    Builds a weighted graph from positive correlations, forms the symmetric
    normalised Laplacian L = I - D^{-1/2} W D^{-1/2}, and analyses its spectrum.
    The Fiedler value (second-smallest eigenvalue) measures how tightly the
    market is connected; the eigengap suggests a natural number of clusters; the
    spectral embedding (``transform``) places assets for clustering.
    """

    target = None
    tier = "A"
    name = "GraphLaplacian"

    def __init__(self, threshold: float = 0.0):
        super().__init__()
        self.threshold = threshold  # drop edges with corr below this
        self._L = None
        self._evals = None
        self._evecs = None

    def fit(self, data) -> "GraphLaplacian":
        R = np.asarray(data.R, dtype=float)
        C = _corr(R)
        W = np.clip(C, self.threshold, None)
        np.fill_diagonal(W, 0.0)
        d = W.sum(axis=1)
        d_inv_sqrt = 1.0 / np.sqrt(np.clip(d, 1e-12, None))
        L = np.eye(W.shape[0]) - (d_inv_sqrt[:, None] * W * d_inv_sqrt[None, :])
        evals, evecs = np.linalg.eigh(L)
        order = np.argsort(evals)
        self._evals = evals[order]
        self._evecs = evecs[:, order]
        self._W = W
        self._L = L
        self._data = data
        self._fitted = True
        return self

    def transform(self, k: int = 2) -> np.ndarray:
        """Spectral embedding: the first k non-trivial Laplacian eigenvectors."""
        self._check_fitted()
        return self._evecs[:, 1 : k + 1]

    def reconstruct(self, data=None) -> np.ndarray:
        """Return the weighted adjacency matrix (the recovered network)."""
        self._check_fitted()
        return self._W

    def suggested_clusters(self) -> int:
        # Largest eigengap among the smallest few eigenvalues.
        ev = self._evals[: min(8, len(self._evals))]
        gaps = np.diff(ev)
        return int(np.argmax(gaps) + 1) if len(gaps) else 1

    def cluster_labels(self) -> np.ndarray:
        """Sign of the Fiedler vector -> two-way spectral partition."""
        self._check_fitted()
        return (self._evecs[:, 1] > 0).astype(int)

    def state(self) -> dict:
        self._check_fitted()
        return {
            "fiedler_value": float(self._evals[1]) if len(self._evals) > 1 else 0.0,
            "laplacian_spectrum": self._evals,
            "suggested_clusters": self.suggested_clusters(),
            "cluster_labels": self.cluster_labels(),
            "adjacency": self._W,
        }


# --------------------------------------------------------------------------- #
# 10. NMF on absolute returns -> co-movement clusters
# --------------------------------------------------------------------------- #
class NMFAbsReturns(BaseModel):
    """Non-negative matrix factorisation of |returns| into co-movement clusters.

        |R| ~ W H,   W >= 0 (T x k activations), H >= 0 (k x n basis patterns)

    Each row of H is a non-negative co-movement pattern over assets; assets are
    assigned to the component that loads on them most. Implemented with
    Lee & Seung multiplicative updates (no ML framework).
    """

    target = None
    tier = "A"
    name = "NMF(|returns|)"

    def __init__(self, k: int = 3, max_iter: int = 300, seed: int = 42):
        super().__init__()
        self.k = k
        self.max_iter = max_iter
        self.seed = seed
        self._W = None
        self._H = None
        self._error = None

    def fit(self, data) -> "NMFAbsReturns":
        X = np.abs(np.asarray(data.R, dtype=float))
        T, n = X.shape
        k = min(self.k, n)
        rng = np.random.default_rng(self.seed)
        W = rng.uniform(0, 1, (T, k)) * np.sqrt(X.mean() / k)
        H = rng.uniform(0, 1, (k, n)) * np.sqrt(X.mean() / k)
        eps = 1e-10
        prev_err = None
        for _ in range(self.max_iter):
            H *= (W.T @ X) / (W.T @ W @ H + eps)
            W *= (X @ H.T) / (W @ (H @ H.T) + eps)
            err = np.linalg.norm(X - W @ H)
            if prev_err is not None and abs(prev_err - err) / (prev_err + eps) < 1e-6:
                break
            prev_err = err
        self._W, self._H, self._error = W, H, float(err)
        self._data = data
        self._fitted = True
        return self

    def transform(self, data=None) -> np.ndarray:
        """Component activations over time, W (T, k)."""
        self._check_fitted()
        return self._W

    def reconstruct(self, data=None) -> np.ndarray:
        """Reconstructed |returns| ~ W H."""
        self._check_fitted()
        return self._W @ self._H

    def cluster_membership(self) -> np.ndarray:
        """Assign each asset to its dominant component (argmax over H columns)."""
        self._check_fitted()
        return np.argmax(self._H, axis=0)

    def state(self) -> dict:
        self._check_fitted()
        Xn = np.linalg.norm(np.abs(self._data.R))
        return {
            "k": self._H.shape[0],
            "components": self._H,
            "reconstruction_error": self._error,
            "relative_error": self._error / (Xn + 1e-12),
            "cluster_membership": self.cluster_membership(),
        }
