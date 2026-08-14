"""data.py - Market data pipeline.

DATA CONTRACT
-------------
Market : US equities, ONE asset class only.
Assets : 15 large-cap S&P 500 names (see ``TICKERS``).
Bars   : daily close.
Range  : ~6 years of daily history (default 2018-01-01 .. 2024-01-01).

Loader resolution order (``load_market_data``):
    1. live fetch via yfinance              (works on an open network)
    2. cached CSV in ./data/                 (committed, reproducible)
    3. seeded synthetic generator            (always available, offline)

Synthetic data is *clearly labelled* (``MarketData.source``) and is built to
mimic real equity markets - common market factor, sector blocks, volatility
clustering, calm/crisis regimes and near-unpredictable direction - so that the
toolkit runs end-to-end and reproducibly even with no network. On a machine
with network access, run ``main.py --refresh`` to pull real prices and overwrite
the cache.

Pipeline guarantees: prices -> log-returns, missing data handled, returns the
matrices ``P`` (T x n) and ``R`` ((T-1) x n).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from core import SEED, logger

# 15 large-cap S&P 500 tickers spanning several sectors (tech, financials,
# energy, staples, healthcare, discretionary) -> genuine cross-sector structure.
TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "XOM",
    "JNJ", "PG", "V", "HD", "BAC", "KO", "DIS",
]

# Coarse sector map used by the synthetic generator to create realistic blocks.
SECTORS = {
    "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "GOOGL": "tech", "META": "tech",
    "AMZN": "discretionary", "HD": "discretionary", "DIS": "discretionary",
    "JPM": "financials", "BAC": "financials", "V": "financials",
    "XOM": "energy",
    "JNJ": "health", "PG": "staples", "KO": "staples",
}

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CACHE = os.path.join(_HERE, "data", "prices_cache.csv")
DEFAULT_START = "2018-01-01"
DEFAULT_END = "2024-01-01"


@dataclass
class MarketData:
    """Container produced by the pipeline and consumed by ``fit(data)``.

    Attributes
    ----------
    P        : price matrix, shape (T, n).
    R        : log-return matrix, shape (T-1, n).
    tickers  : list of n asset symbols (column order of P and R).
    dates    : DatetimeIndex of length T (price dates).
    source   : "yfinance" | "cache" | "synthetic".
    """

    P: np.ndarray
    R: np.ndarray
    tickers: list[str]
    dates: pd.DatetimeIndex
    source: str
    meta: dict = field(default_factory=dict)

    @property
    def T(self) -> int:
        return self.P.shape[0]

    @property
    def n(self) -> int:
        return self.P.shape[1]

    def __repr__(self) -> str:
        return (
            f"MarketData(source={self.source!r}, T={self.T}, n={self.n}, "
            f"tickers={self.tickers[:3]}...+{self.n - 3})"
        )


def log_returns(P: np.ndarray) -> np.ndarray:
    """Log-returns from a price matrix. Shape (T-1, n)."""
    P = np.asarray(P, dtype=float)
    return np.diff(np.log(P), axis=0)


def _clean_prices(df: pd.DataFrame) -> pd.DataFrame:
    """Handle missing data WITHOUT look-ahead.

    Forward-fill internal gaps (holidays, halts) using only past prices. We do
    NOT back-fill: back-filling leading NaNs would copy a *future* price backward
    into the period before an asset started trading - a look-ahead leak on real
    data where tickers have different inception dates. Leading NaNs are instead
    dropped, so the panel starts when every asset has data.
    """
    df = df.sort_index()
    # Drop columns that are entirely missing.
    df = df.dropna(axis=1, how="all")
    # Forward-fill only (past -> present). Never backward (future -> past).
    df = df.ffill()
    # Drop leading rows where any asset has no price yet (no back-fill).
    df = df.dropna(axis=0, how="any")
    # Drop non-positive prices (cannot take a log).
    df = df[(df > 0).all(axis=1)]
    return df


def _build(df: pd.DataFrame, source: str) -> MarketData:
    df = _clean_prices(df)
    if df.shape[0] < 30:
        raise ValueError(f"Not enough usable price rows ({df.shape[0]}) from source {source!r}.")
    P = df.to_numpy(dtype=float)
    R = log_returns(P)
    md = MarketData(
        P=P,
        R=R,
        tickers=list(df.columns),
        dates=df.index,
        source=source,
        meta={"start": str(df.index[0].date()), "end": str(df.index[-1].date())},
    )
    logger.info("Loaded %s | source=%s | T=%d n=%d", md, source, md.T, md.n)
    return md


# --------------------------------------------------------------------------- #
# 1. live fetch
# --------------------------------------------------------------------------- #
def _fetch_yfinance(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    import yfinance as yf  # imported lazily so the toolkit has no hard dependency

    raw = yf.download(tickers, start=start, end=end, progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0:
        raise RuntimeError("yfinance returned no data (blocked network or bad symbols).")
    # With multiple tickers yfinance returns a column MultiIndex (field, ticker).
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:  # single ticker
        close = raw[["Close"]]
        close.columns = tickers
    close = close[[t for t in tickers if t in close.columns]]
    if close.shape[1] == 0:
        raise RuntimeError("yfinance returned no Close columns for the requested tickers.")
    return close


# --------------------------------------------------------------------------- #
# 3. synthetic generator
# --------------------------------------------------------------------------- #
def generate_synthetic_prices(
    tickers: list[str] | None = None,
    T: int = 1500,
    start: str = DEFAULT_START,
    seed: int = SEED,
    crisis_drift: float = -0.0012,
) -> pd.DataFrame:
    """Seeded synthetic daily prices that mimic real equity markets.

    Structure deliberately baked in so the toolkit has something to find:
      * a dominant MARKET factor (so PCA/RMT see one big eigenvalue);
      * SECTOR factors (so eigen-portfolios / NMF clusters are meaningful);
      * a 2-state calm/crisis REGIME (so HMM / switching models work and the
        correlation change-detector fires on the calm->crisis transition);
      * VOLATILITY CLUSTERING via an AR(1) log-vol process (so EWMA beats the
        rolling-mean null);
      * NEAR-ZERO predictable drift and i.i.d.-in-sign innovations (so the
        Tier-D direction models correctly fail to beat the coin flip).
    """
    tickers = tickers or TICKERS
    n = len(tickers)
    rng = np.random.default_rng(seed)

    # --- regime path: 2-state Markov chain (0 = calm, 1 = crisis) ----------
    # Persistent regimes; crises are rarer and shorter.
    P_trans = np.array([[0.985, 0.015], [0.06, 0.94]])
    regime = np.zeros(T, dtype=int)
    for t in range(1, T):
        regime[t] = rng.choice([0, 1], p=P_trans[regime[t - 1]])

    # --- common volatility level scales with regime ------------------------
    base_vol = np.where(regime == 0, 0.0042, 0.011)  # daily market-factor vol

    # --- AR(1) log-vol multiplier (clustering) per asset -------------------
    # De-meaned so exp() does not inflate the unconditional variance.
    phi = 0.94
    sig_eta = 0.22
    log_vol = np.zeros((T, n))
    for t in range(1, T):
        log_vol[t] = phi * log_vol[t - 1] + sig_eta * rng.standard_normal(n)
    log_vol -= 0.5 * sig_eta ** 2 / (1 - phi ** 2)  # centre exp(log_vol) near 1
    idio_vol = np.exp(log_vol) * np.where(regime == 0, 0.0095, 0.017)[:, None]

    # --- factor loadings ---------------------------------------------------
    market_beta = rng.uniform(0.8, 1.3, n)
    sector_names = sorted(set(SECTORS.get(t, "other") for t in tickers))
    sector_idx = np.array([sector_names.index(SECTORS.get(t, "other")) for t in tickers])
    n_sectors = len(sector_names)
    sector_beta = rng.uniform(0.3, 0.7, n)

    # --- factor innovations ------------------------------------------------
    market_factor = rng.standard_normal(T) * base_vol
    sector_factor = rng.standard_normal((T, n_sectors)) * (base_vol[:, None] * 0.6)
    # Crisis raises cross-sectional correlation by loading more on the market.
    crisis_boost = np.where(regime == 1, 1.6, 1.0)

    # --- assemble returns --------------------------------------------------
    # A SINGLE small common drift (a market-wide risk premium, ~+5%/yr) and NO
    # per-asset idiosyncratic drift. Per-asset drift dispersion would be a stable,
    # learnable directional signal - a synthetic artifact that would let the
    # Tier-D models "predict" direction. Keeping drift common and tiny means
    # daily direction is genuinely near-unpredictable (the core thesis), while
    # the base-rate null absorbs the common premium.
    drift = np.full(n, 0.0006)  # calm-period drift; the risk premium earned ex-crises
    # crises are high-vol AND negative-return (the "leverage effect" of real
    # markets): the risk premium is earned in calm periods and given back in
    # crashes. This is what makes volatility management pay off - and it is added
    # as a REGIME-level effect, so day-to-day direction stays ~unpredictable.
    crisis_ret = np.where(regime == 1, crisis_drift, 0.0)
    R = np.zeros((T, n))
    for t in range(T):
        mkt = market_beta * market_factor[t] * crisis_boost[t]
        sec = sector_beta * sector_factor[t, sector_idx]
        idio = idio_vol[t] * rng.standard_normal(n)
        R[t] = drift + crisis_ret[t] + mkt + sec + idio

    # --- prices ------------------------------------------------------------
    start_prices = rng.uniform(40, 320, n)
    P = start_prices * np.exp(np.cumsum(R, axis=0))

    dates = pd.bdate_range(start=start, periods=T)
    df = pd.DataFrame(P, index=dates, columns=tickers)
    df.index.name = "Date"
    return df


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def load_market_data(
    tickers: list[str] | None = None,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    cache_path: str = DEFAULT_CACHE,
    refresh: bool = False,
    allow_synthetic: bool = True,
    synthetic_days: int | None = None,
    force_synthetic: bool = False,
) -> MarketData:
    """Load prices and produce ``MarketData`` (P, R, tickers, dates, source).

    Parameters
    ----------
    refresh : if True, try a live yfinance fetch first and overwrite the cache.
    allow_synthetic : if True, fall back to the seeded synthetic generator when
        neither network nor cache is available.
    synthetic_days : length of the synthetic series to generate (default 1500).
    force_synthetic : skip live + cache and generate a fresh synthetic panel of
        ``synthetic_days`` rows (used for the "max data" scale-up runs). Cached to
        a size-specific file so the committed canonical cache is never clobbered.
    """
    tickers = tickers or TICKERS

    # 0. forced synthetic of an arbitrary size (the "train on as much as possible"
    #    path when no real data is reachable). Reproducible: seeded, size-keyed cache.
    if force_synthetic:
        days = synthetic_days or 1500
        sized_cache = os.path.join(os.path.dirname(cache_path), f"prices_synth_{days}.csv")
        if os.path.exists(sized_cache) and not refresh:
            df = pd.read_csv(sized_cache, index_col=0, parse_dates=True)
            return _build(df, source="synthetic")
        logger.warning("Generating %d-day SYNTHETIC panel (no real data reachable).", days)
        df = generate_synthetic_prices(tickers, T=days, start=start)
        os.makedirs(os.path.dirname(sized_cache), exist_ok=True)
        df.to_csv(sized_cache)
        return _build(df, source="synthetic")

    # 1. live fetch (only when explicitly refreshing or no cache exists)
    if refresh or not os.path.exists(cache_path):
        try:
            close = _fetch_yfinance(tickers, start, end)
            md = _build(close, source="yfinance")
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            close.to_csv(cache_path)
            logger.info("Cached real prices to %s", cache_path)
            return md
        except Exception as exc:  # network blocked, symbol error, etc.
            logger.warning("Live fetch failed (%s). Falling back to cache/synthetic.", exc)

    # 2. cached CSV
    if os.path.exists(cache_path):
        try:
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            df = df[[t for t in tickers if t in df.columns]]
            # Only real (yfinance-fetched) prices are ever written to this path;
            # the synthetic fallback caches to its own file (step 3 below).
            return _build(df, source="cache")
        except Exception as exc:
            logger.warning("Cache read failed (%s).", exc)

    # 3. synthetic fallback
    if allow_synthetic:
        logger.warning("Using SYNTHETIC data (no network, no cache). Results are illustrative.")
        # Never write synthetic prices to ``cache_path``: that file is documented
        # as cached REAL prices, and a later run would reload it with
        # source="cache", silently presenting synthetic data as market data.
        # Keep the fallback in its own clearly-named file (as force_synthetic
        # does) so it is always reloaded with source="synthetic".
        synth_cache = os.path.join(os.path.dirname(cache_path), "prices_synth_fallback.csv")
        if os.path.exists(synth_cache) and not refresh:
            df = pd.read_csv(synth_cache, index_col=0, parse_dates=True)
            return _build(df, source="synthetic")
        df = generate_synthetic_prices(tickers, start=start)
        os.makedirs(os.path.dirname(synth_cache), exist_ok=True)
        df.to_csv(synth_cache)
        return _build(df, source="synthetic")

    raise RuntimeError("No data available: network blocked, no cache, synthetic disabled.")


if __name__ == "__main__":  # quick smoke test
    md = load_market_data()
    print(md)
    print("P", md.P.shape, "R", md.R.shape, "source", md.source)
    print("mean daily vol:", np.std(md.R, axis=0).mean())
