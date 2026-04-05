#!/usr/bin/env python3
"""
Enhanced Market Data Module for Stock Prediction

Provides real-time and near-real-time market signals that complement
historical technical features: pre-market futures, international closes,
sector/peer momentum, macro indicators, and options-derived signals.

All functions return dicts of floats suitable for flattening into an ML
feature vector.  Every function is wrapped in error handling so that a
data-fetch failure never crashes the caller -- sensible defaults (0.0)
are returned instead.

Cache layer: module-level dict with 30-minute TTL so repeated calls
within the same session don't hammer the API.
"""

import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

import numpy as np

try:
    import yfinance as yf
except ImportError:
    import subprocess
    subprocess.check_call(["pip3", "install", "yfinance", "-q"])
    import yfinance as yf

# ---------------------------------------------------------------------------
# Cache infrastructure
# ---------------------------------------------------------------------------

_cache: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL = 30 * 60  # 30 minutes in seconds


def _cache_get(key: str) -> Optional[Any]:
    """Return cached value if present and not expired, else None."""
    entry = _cache.get(key)
    if entry is None:
        return None
    if time.time() - entry["ts"] > _CACHE_TTL:
        del _cache[key]
        return None
    return entry["value"]


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = {"ts": time.time(), "value": value}


# ---------------------------------------------------------------------------
# Ticker -> sector / peer mappings
# ---------------------------------------------------------------------------

SECTOR_ETF_MAP: Dict[str, str] = {
    "AAPL": "XLK",
    "MSFT": "XLK",
    "GOOGL": "XLK",
    "GOOG": "XLK",
    "NVDA": "XLK",
    "META": "XLC",
    "AMZN": "XLY",
    "TSLA": "XLY",
    "AMD": "XLK",
    "INTC": "XLK",
    "AVGO": "XLK",
    "NFLX": "XLC",
}

PEER_MAP: Dict[str, List[str]] = {
    "AAPL": ["MSFT", "GOOGL", "META"],
    "MSFT": ["AAPL", "GOOGL", "META"],
    "GOOGL": ["META", "MSFT", "AAPL"],
    "NVDA": ["AMD", "AVGO", "INTC"],
    "TSLA": ["F", "GM", "RIVN"],
    "AMZN": ["WMT", "TGT", "SHOP"],
    "META": ["GOOGL", "SNAP", "PINS"],
    "AMD": ["NVDA", "INTC", "AVGO"],
}

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _safe_pct_change(current: float, previous: float) -> float:
    """Return percentage change; 0.0 if previous is zero or missing."""
    if previous is None or previous == 0:
        return 0.0
    return (current - previous) / abs(previous) * 100.0


def _fetch_recent_history(symbol: str, period: str = "5d", interval: str = "1d"):
    """Download recent OHLCV via yfinance, return DataFrame or None."""
    try:
        tk = yf.Ticker(symbol)
        hist = tk.history(period=period, interval=interval)
        if hist is None or hist.empty:
            return None
        return hist
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. Pre-market / futures data
# ---------------------------------------------------------------------------


def get_premarket_futures_data(tickers: List[str]) -> Dict[str, Dict[str, float]]:
    """
    For each ticker, fetch pre-market change %.
    Also fetch S&P 500 futures (ES=F) and Nasdaq futures (NQ=F) change
    vs their previous close.

    Returns dict of ticker -> {premarket_change_pct, futures_es_change,
                                futures_nq_change}
    """
    result: Dict[str, Dict[str, float]] = {}

    # --- Futures -----------------------------------------------------------
    es_change = 0.0
    nq_change = 0.0
    try:
        for sym, name in [("ES=F", "es"), ("NQ=F", "nq")]:
            hist = _fetch_recent_history(sym, period="5d", interval="1d")
            if hist is not None and len(hist) >= 2:
                prev_close = float(hist["Close"].iloc[-2])
                last_close = float(hist["Close"].iloc[-1])
                change = _safe_pct_change(last_close, prev_close)
                if name == "es":
                    es_change = change
                else:
                    nq_change = change
    except Exception:
        pass

    # --- Per-ticker pre-market ---------------------------------------------
    for ticker in tickers:
        pm_change = 0.0
        try:
            tk = yf.Ticker(ticker)
            info = tk.info or {}
            pre_price = info.get("preMarketPrice")
            prev_close = info.get("regularMarketPreviousClose") or info.get("previousClose")
            if pre_price and prev_close:
                pm_change = _safe_pct_change(float(pre_price), float(prev_close))
        except Exception:
            pass

        result[ticker] = {
            "premarket_change_pct": round(pm_change, 4),
            "futures_es_change": round(es_change, 4),
            "futures_nq_change": round(nq_change, 4),
        }

    return result


# ---------------------------------------------------------------------------
# 2. International market signals
# ---------------------------------------------------------------------------

_INTL_MARKETS = {
    "nikkei": "^N225",
    "dax": "^GDAXI",
    "ftse": "^FTSE",
    "hang_seng": "^HSI",
    "shanghai": "000001.SS",
}


def get_international_signals() -> Dict[str, float]:
    """
    Get most recent daily return for major international indices.
    Returns dict of market_name -> return_pct.
    """
    cached = _cache_get("international_signals")
    if cached is not None:
        return cached

    result: Dict[str, float] = {}
    for name, symbol in _INTL_MARKETS.items():
        try:
            hist = _fetch_recent_history(symbol, period="5d", interval="1d")
            if hist is not None and len(hist) >= 2:
                prev = float(hist["Close"].iloc[-2])
                last = float(hist["Close"].iloc[-1])
                result[name] = round(_safe_pct_change(last, prev), 4)
            else:
                result[name] = 0.0
        except Exception:
            result[name] = 0.0

    _cache_set("international_signals", result)
    return result


# ---------------------------------------------------------------------------
# 3. Sector / peer signals
# ---------------------------------------------------------------------------


def get_sector_peer_data(ticker: str) -> Dict[str, float]:
    """
    Get sector ETF momentum (1d, 5d) and peer-stock average 1d return
    for the given ticker.

    Returns dict with keys: sector_1d_ret, sector_5d_ret, peer_avg_1d_ret
    """
    cache_key = f"sector_peer_{ticker}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    defaults = {"sector_1d_ret": 0.0, "sector_5d_ret": 0.0, "peer_avg_1d_ret": 0.0}

    # --- Sector ETF --------------------------------------------------------
    etf_sym = SECTOR_ETF_MAP.get(ticker, "SPY")  # fallback to SPY
    try:
        hist = _fetch_recent_history(etf_sym, period="1mo", interval="1d")
        if hist is not None and len(hist) >= 6:
            closes = hist["Close"]
            defaults["sector_1d_ret"] = round(
                _safe_pct_change(float(closes.iloc[-1]), float(closes.iloc[-2])), 4
            )
            defaults["sector_5d_ret"] = round(
                _safe_pct_change(float(closes.iloc[-1]), float(closes.iloc[-6])), 4
            )
        elif hist is not None and len(hist) >= 2:
            closes = hist["Close"]
            defaults["sector_1d_ret"] = round(
                _safe_pct_change(float(closes.iloc[-1]), float(closes.iloc[-2])), 4
            )
    except Exception:
        pass

    # --- Peer average 1d return --------------------------------------------
    peers = PEER_MAP.get(ticker, [])
    peer_rets: List[float] = []
    for p in peers:
        try:
            hist = _fetch_recent_history(p, period="5d", interval="1d")
            if hist is not None and len(hist) >= 2:
                prev = float(hist["Close"].iloc[-2])
                last = float(hist["Close"].iloc[-1])
                peer_rets.append(_safe_pct_change(last, prev))
        except Exception:
            continue

    if peer_rets:
        defaults["peer_avg_1d_ret"] = round(float(np.mean(peer_rets)), 4)

    _cache_set(cache_key, defaults)
    return defaults


# ---------------------------------------------------------------------------
# 4. Macro data
# ---------------------------------------------------------------------------

_MACRO_SYMBOLS = {
    "treasury_10y": "^TNX",
    "usd_index": "DX-Y.NYB",
    "crude_oil": "CL=F",
    "gold": "GC=F",
}


def get_macro_data() -> Dict[str, Dict[str, float]]:
    """
    Fetch macro signals: 10Y yield, USD index, crude oil, gold.
    Each entry has 'level' and 'change_1d'.
    """
    cached = _cache_get("macro_data")
    if cached is not None:
        return cached

    result: Dict[str, Dict[str, float]] = {}
    for name, symbol in _MACRO_SYMBOLS.items():
        entry = {"level": 0.0, "change_1d": 0.0}
        try:
            hist = _fetch_recent_history(symbol, period="5d", interval="1d")
            if hist is not None and len(hist) >= 2:
                last = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2])
                entry["level"] = round(last, 4)
                entry["change_1d"] = round(_safe_pct_change(last, prev), 4)
            elif hist is not None and len(hist) >= 1:
                entry["level"] = round(float(hist["Close"].iloc[-1]), 4)
        except Exception:
            pass
        result[name] = entry

    _cache_set("macro_data", result)
    return result


# ---------------------------------------------------------------------------
# 5. Options-derived signals
# ---------------------------------------------------------------------------


def get_options_signals(ticker_yf: str) -> Dict[str, float]:
    """
    Derive put/call ratios and average implied volatility from the
    nearest-expiry option chain.

    Returns dict with keys: pcr_volume, pcr_oi, avg_iv
    """
    defaults = {"pcr_volume": 0.0, "pcr_oi": 0.0, "avg_iv": 0.0}

    try:
        tk = yf.Ticker(ticker_yf)
        expirations = tk.options
        if not expirations:
            return defaults

        # Use the nearest expiration
        chain = tk.option_chain(expirations[0])
        calls = chain.calls
        puts = chain.puts

        if calls is None or puts is None or calls.empty or puts.empty:
            return defaults

        # Put/call volume ratio
        total_call_vol = calls["volume"].sum()
        total_put_vol = puts["volume"].sum()
        if total_call_vol > 0:
            defaults["pcr_volume"] = round(float(total_put_vol / total_call_vol), 4)

        # Put/call open interest ratio
        total_call_oi = calls["openInterest"].sum()
        total_put_oi = puts["openInterest"].sum()
        if total_call_oi > 0:
            defaults["pcr_oi"] = round(float(total_put_oi / total_call_oi), 4)

        # Average implied volatility of near-ATM options
        # Find the current price and select strikes within 5% of it
        try:
            info = tk.info or {}
            current_price = info.get("regularMarketPrice") or info.get("currentPrice", 0)
            if current_price and current_price > 0:
                lo = current_price * 0.95
                hi = current_price * 1.05
                atm_calls = calls[
                    (calls["strike"] >= lo) & (calls["strike"] <= hi)
                ]
                atm_puts = puts[
                    (puts["strike"] >= lo) & (puts["strike"] <= hi)
                ]
                ivs = []
                if not atm_calls.empty and "impliedVolatility" in atm_calls.columns:
                    ivs.extend(atm_calls["impliedVolatility"].dropna().tolist())
                if not atm_puts.empty and "impliedVolatility" in atm_puts.columns:
                    ivs.extend(atm_puts["impliedVolatility"].dropna().tolist())
                if ivs:
                    defaults["avg_iv"] = round(float(np.mean(ivs)), 4)
        except Exception:
            pass

    except Exception:
        pass

    return defaults


# ---------------------------------------------------------------------------
# 6. Master builder
# ---------------------------------------------------------------------------


def build_enhanced_features(
    ticker_key: str,
    ticker_yf: str,
    lookback_days: int = 60,
) -> Dict[str, float]:
    """
    Aggregate all enhanced signals into a single flat dict for one ticker.
    Keys are prefixed by category so they can be safely merged into an ML
    feature vector.

    Parameters
    ----------
    ticker_key : str
        Short identifier used in mappings (e.g. "AAPL", "NVDA").
    ticker_yf : str
        Yahoo Finance symbol (usually same as ticker_key).
    lookback_days : int
        Not directly used here but reserved for callers that want to
        control history depth.

    Returns
    -------
    dict of str -> float
    """
    features: Dict[str, float] = {}

    # 1. Pre-market / futures
    try:
        pm = get_premarket_futures_data([ticker_key])
        pm_data = pm.get(ticker_key, {})
        features["pm_premarket_change_pct"] = pm_data.get("premarket_change_pct", 0.0)
        features["pm_futures_es_change"] = pm_data.get("futures_es_change", 0.0)
        features["pm_futures_nq_change"] = pm_data.get("futures_nq_change", 0.0)
    except Exception:
        features["pm_premarket_change_pct"] = 0.0
        features["pm_futures_es_change"] = 0.0
        features["pm_futures_nq_change"] = 0.0

    # 2. International signals
    try:
        intl = get_international_signals()
        for mkt, ret in intl.items():
            features[f"intl_{mkt}_ret"] = ret
    except Exception:
        for mkt in _INTL_MARKETS:
            features[f"intl_{mkt}_ret"] = 0.0

    # 3. Sector / peer
    try:
        sp = get_sector_peer_data(ticker_key)
        features["sector_1d_ret"] = sp.get("sector_1d_ret", 0.0)
        features["sector_5d_ret"] = sp.get("sector_5d_ret", 0.0)
        features["peer_avg_1d_ret"] = sp.get("peer_avg_1d_ret", 0.0)
    except Exception:
        features["sector_1d_ret"] = 0.0
        features["sector_5d_ret"] = 0.0
        features["peer_avg_1d_ret"] = 0.0

    # 4. Macro
    try:
        macro = get_macro_data()
        for ind, vals in macro.items():
            features[f"macro_{ind}_level"] = vals.get("level", 0.0)
            features[f"macro_{ind}_change_1d"] = vals.get("change_1d", 0.0)
    except Exception:
        for ind in _MACRO_SYMBOLS:
            features[f"macro_{ind}_level"] = 0.0
            features[f"macro_{ind}_change_1d"] = 0.0

    # 5. Options
    try:
        opts = get_options_signals(ticker_yf)
        features["opt_pcr_volume"] = opts.get("pcr_volume", 0.0)
        features["opt_pcr_oi"] = opts.get("pcr_oi", 0.0)
        features["opt_avg_iv"] = opts.get("avg_iv", 0.0)
    except Exception:
        features["opt_pcr_volume"] = 0.0
        features["opt_pcr_oi"] = 0.0
        features["opt_avg_iv"] = 0.0

    return features


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    TEST_TICKERS = ["AAPL", "NVDA", "TSLA"]

    print("=" * 70)
    print("Enhanced Data Module — Self-Test")
    print("=" * 70)

    # 1. Pre-market / futures
    print("\n--- 1. Pre-market & Futures Data ---")
    try:
        pm = get_premarket_futures_data(TEST_TICKERS)
        pprint.pprint(pm)
    except Exception as e:
        print(f"  ERROR: {e}")

    # 2. International signals
    print("\n--- 2. International Market Signals ---")
    try:
        intl = get_international_signals()
        pprint.pprint(intl)
    except Exception as e:
        print(f"  ERROR: {e}")

    # 3. Sector / peer data
    print("\n--- 3. Sector & Peer Data ---")
    for t in TEST_TICKERS:
        try:
            sp = get_sector_peer_data(t)
            print(f"  {t}: {sp}")
        except Exception as e:
            print(f"  {t} ERROR: {e}")

    # 4. Macro data
    print("\n--- 4. Macro Data ---")
    try:
        macro = get_macro_data()
        pprint.pprint(macro)
    except Exception as e:
        print(f"  ERROR: {e}")

    # 5. Options signals
    print("\n--- 5. Options Signals ---")
    for t in TEST_TICKERS:
        try:
            opts = get_options_signals(t)
            print(f"  {t}: {opts}")
        except Exception as e:
            print(f"  {t} ERROR: {e}")

    # 6. Full enhanced feature vector
    print("\n--- 6. Full Enhanced Feature Vector (NVDA) ---")
    try:
        feats = build_enhanced_features("NVDA", "NVDA")
        pprint.pprint(feats)
        print(f"\n  Total features: {len(feats)}")
        print(f"  All values numeric: {all(isinstance(v, (int, float)) for v in feats.values())}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print("\n" + "=" * 70)
    print("Self-test complete.")
    print("=" * 70)
