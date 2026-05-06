#!/usr/bin/env python3
"""
Long-term holding analytics for CryptoEdge.

This module is the "long horizon" lens on the same assets the rest of the
dashboard tracks short-term. It deliberately ignores the 5-minute / 1-second
pipeline and works on **daily bars** plus **on-chain fundamentals**, which is
what matters when the holding period is measured in months or years.

Data sources (all free):
  - Binance daily klines (already used elsewhere) — daily OHLCV
  - CoinMetrics Community API (no key, BTC/ETH coverage) — on-chain metrics
  - Optional: Glassnode (set GLASSNODE_API_KEY) for the full BTC/ETH suite

Analytics:
  - 200WMA + Mayer multiple (cycle phase)
  - Realized drawdown, Sharpe, Sortino, vol regime
  - MVRV / NVT proxies (from supply + market cap if available, else price-only)
  - DCA-with-fear-multiplier recommender
  - Drift-band rebalance recommender against target weights
  - Risk-off composite signal

Everything in this module is pure-ish: it reads from Binance/CoinMetrics over
HTTP and from the SQLite cache, but it does not depend on the live tick
pipeline, so it works even when the short-term analyzer is still warming up.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import requests

import database as db

log = logging.getLogger("crypto.long_term")

CACHE_DIR = Path(__file__).parent / "cache" / "long_term"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
COINMETRICS_BASE = "https://community-api.coinmetrics.io/v4"
GLASSNODE_BASE = "https://api.glassnode.com/v1/metrics"

# Map our internal tickers to the symbol conventions of each data source.
TICKER_MAP = {
    "BTC":  {"binance": "BTCUSDT",  "coinmetrics": "btc",  "glassnode": "BTC"},
    "ETH":  {"binance": "ETHUSDT",  "coinmetrics": "eth",  "glassnode": "ETH"},
    "SOL":  {"binance": "SOLUSDT",  "coinmetrics": "sol",  "glassnode": "SOL"},
    "DOGE": {"binance": "DOGEUSDT", "coinmetrics": "doge", "glassnode": "DOGE"},
    "XRP":  {"binance": "XRPUSDT",  "coinmetrics": "xrp",  "glassnode": "XRP"},
}

# CoinMetrics has the richest free coverage on BTC and ETH. The other three
# only get price-based analytics (200WMA, drawdown, vol regime, etc.), which
# is still everything you need for cycle-aware DCA.
ONCHAIN_COVERED = {"BTC", "ETH"}

# Risk-free rate (annualized) used in Sharpe. Tunable via env if you really
# care, but for crypto it's noise next to vol so the default is fine.
RF_RATE = float(os.environ.get("LONG_TERM_RF_RATE", "0.04"))

DAILY_FETCH_LIMIT = 1000  # Binance kline limit
HISTORY_DAYS_DEFAULT = 365 * 4  # 4 years covers the last halving cycle


# ─── Daily bar fetch + cache ────────────────────────────────────────────────

def _binance_daily_klines(symbol: str, start_ms: int, end_ms: int) -> list[list]:
    """Fetch daily klines from Binance. Paginates by 1000-day chunks."""
    out: list[list] = []
    cur = start_ms
    chunk_ms = DAILY_FETCH_LIMIT * 24 * 3600 * 1000
    while cur < end_ms:
        ce = min(cur + chunk_ms, end_ms)
        params = {
            "symbol": symbol, "interval": "1d",
            "startTime": cur, "endTime": ce, "limit": DAILY_FETCH_LIMIT,
        }
        for attempt in range(3):
            try:
                r = requests.get(BINANCE_KLINE_URL, params=params, timeout=20)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", "5")))
                    continue
                r.raise_for_status()
                out.extend(r.json())
                break
            except requests.RequestException as e:
                if attempt == 2:
                    log.warning("binance daily fetch failed for %s: %s", symbol, e)
                    return out
                time.sleep(1 + attempt)
        cur = ce
    return out


def refresh_daily_bars(ticker: str, days: int = HISTORY_DAYS_DEFAULT) -> int:
    """Fetch missing daily bars and upsert into crypto_daily_bars.
    Returns the number of new rows inserted."""
    info = TICKER_MAP.get(ticker)
    if not info:
        return 0

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000

    # Skip the chunks we already have. crypto_daily_bars stores ISO date as PK.
    last = db.get_latest_daily_bar_date(ticker)
    if last:
        last_ms = int(datetime.fromisoformat(last).replace(tzinfo=timezone.utc).timestamp() * 1000)
        # Re-fetch the last day to keep close updated (today's bar isn't final).
        start_ms = max(start_ms, last_ms - 24 * 3600 * 1000)

    raw = _binance_daily_klines(info["binance"], start_ms, end_ms)
    rows = []
    for k in raw:
        d = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).date().isoformat()
        rows.append((ticker, d, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])))
    if rows:
        db.upsert_daily_bars(rows)
    return len(rows)


def get_daily_closes(ticker: str, days: int = 365 * 4) -> tuple[list[str], np.ndarray]:
    """Return (iso_dates, closes) sorted oldest→newest."""
    rows = db.get_daily_bars(ticker, days=days)
    dates = [r["date"] for r in rows]
    closes = np.asarray([r["close"] for r in rows], dtype=np.float64)
    return dates, closes


# ─── On-chain metrics (CoinMetrics Community + optional Glassnode) ──────────

# CoinMetrics metric IDs we pull. These are all on the Community tier (free).
CM_METRICS = [
    "PriceUSD",       # daily close in USD
    "CapMrktCurUSD",  # market cap (price × supply)
    "CapRealUSD",     # realized cap (sum of UTXOs at price-when-last-moved)
    "TxTfrValAdjUSD", # adjusted transfer volume in USD (NVT denominator)
    "SplyCur",        # current supply
    "FlowOutExNtv",   # native units flowing out of exchanges (BTC has it; others may not)
    "AdrActCnt",      # active addresses
    "HashRate",       # only for PoW chains (BTC); ETH post-merge will be null
]


def _coinmetrics_fetch(asset: str, metrics: list[str], start: str, end: str) -> dict[str, list[dict]]:
    """One paginated call against the Community API. Returns {metric_id: [rows]}.
    Rows look like {time: "2024-01-01T00:00:00.000Z", value: "..."}.
    """
    by_metric: dict[str, list[dict]] = {m: [] for m in metrics}
    page_token = None
    for _ in range(10):  # hard cap on pagination loops
        params = {
            "assets": asset,
            "metrics": ",".join(metrics),
            "frequency": "1d",
            "start_time": start,
            "end_time": end,
            "page_size": 1000,
        }
        if page_token:
            params["next_page_token"] = page_token
        try:
            r = requests.get(f"{COINMETRICS_BASE}/timeseries/asset-metrics", params=params, timeout=30)
            if r.status_code == 404:
                # Asset/metric combo not supported — that's fine, we degrade gracefully.
                return by_metric
            r.raise_for_status()
            payload = r.json()
        except requests.RequestException as e:
            log.warning("coinmetrics fetch failed for %s: %s", asset, e)
            return by_metric

        for row in payload.get("data", []):
            for m in metrics:
                v = row.get(m)
                if v is None:
                    continue
                by_metric[m].append({"time": row["time"], "value": v})
        page_token = payload.get("next_page_token")
        if not page_token:
            break
    return by_metric


def refresh_onchain_metrics(ticker: str, days: int = HISTORY_DAYS_DEFAULT) -> int:
    """Fetch on-chain metrics from CoinMetrics and upsert. Returns row count."""
    if ticker not in ONCHAIN_COVERED:
        return 0
    info = TICKER_MAP[ticker]
    end = datetime.now(timezone.utc).date().isoformat()
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

    # Honour what we already have so we don't re-fetch 4 years every refresh.
    latest = db.get_latest_onchain_date(ticker)
    if latest:
        start = (datetime.fromisoformat(latest) + timedelta(days=1)).date().isoformat()
        if start >= end:
            return 0

    by_metric = _coinmetrics_fetch(info["coinmetrics"], CM_METRICS, start, end)
    rows = []
    for metric, points in by_metric.items():
        for p in points:
            try:
                d = p["time"][:10]  # YYYY-MM-DD
                rows.append((ticker, metric, d, float(p["value"])))
            except (KeyError, TypeError, ValueError):
                continue
    if rows:
        db.upsert_onchain_metrics(rows)
    return len(rows)


def get_onchain_series(ticker: str, metric: str, days: int = 365) -> tuple[list[str], np.ndarray]:
    """Return (iso_dates, values) for a single on-chain metric."""
    rows = db.get_onchain_metric(ticker, metric, days=days)
    dates = [r["date"] for r in rows]
    vals = np.asarray([r["value"] for r in rows], dtype=np.float64)
    return dates, vals


# ─── Core analytics ─────────────────────────────────────────────────────────

def _safe_log_returns(closes: np.ndarray) -> np.ndarray:
    if len(closes) < 2:
        return np.array([])
    return np.diff(np.log(closes))


def sharpe_ratio(closes: np.ndarray, periods_per_year: int = 365) -> float:
    """Annualized Sharpe on daily log returns."""
    r = _safe_log_returns(closes)
    if len(r) < 30:
        return float("nan")
    excess = r - (RF_RATE / periods_per_year)
    sd = float(np.std(excess, ddof=1))
    if sd == 0:
        return float("nan")
    return float(np.mean(excess) / sd * math.sqrt(periods_per_year))


def sortino_ratio(closes: np.ndarray, periods_per_year: int = 365) -> float:
    r = _safe_log_returns(closes)
    if len(r) < 30:
        return float("nan")
    excess = r - (RF_RATE / periods_per_year)
    downside = excess[excess < 0]
    if len(downside) < 5:
        return float("nan")
    dd = float(np.sqrt(np.mean(downside ** 2)))
    if dd == 0:
        return float("nan")
    return float(np.mean(excess) / dd * math.sqrt(periods_per_year))


def max_drawdown(closes: np.ndarray) -> dict:
    """Worst peak-to-trough drawdown over the series.
    Returns peak/trough indices, the dd as a fraction, and recovery state."""
    if len(closes) < 2:
        return {"max_dd": 0.0, "peak_idx": 0, "trough_idx": 0, "recovered": True, "current_dd": 0.0}
    running_peak = np.maximum.accumulate(closes)
    dd = (closes - running_peak) / running_peak
    trough_idx = int(np.argmin(dd))
    peak_idx = int(np.argmax(closes[:trough_idx + 1])) if trough_idx > 0 else 0
    max_dd_val = float(dd[trough_idx])
    current_dd = float((closes[-1] - running_peak[-1]) / running_peak[-1])
    recovered = closes[-1] >= running_peak[trough_idx]
    return {
        "max_dd": max_dd_val,
        "peak_idx": peak_idx,
        "trough_idx": trough_idx,
        "recovered": bool(recovered),
        "current_dd": current_dd,
    }


def realized_volatility(closes: np.ndarray, window: int = 30, annualize: bool = True) -> float:
    """Realized vol of daily log returns over the trailing `window` days."""
    r = _safe_log_returns(closes)
    if len(r) < window:
        return float("nan")
    sd = float(np.std(r[-window:], ddof=1))
    if annualize:
        sd *= math.sqrt(365)
    return sd


def vol_regime(closes: np.ndarray) -> str:
    """Classify the current 30d vol vs the 1y distribution.
    Returns 'low', 'normal', 'elevated', or 'extreme'."""
    rv30 = realized_volatility(closes, window=30, annualize=True)
    if math.isnan(rv30):
        return "unknown"
    # Build the vol distribution: rolling 30d annualized vol over the last year.
    r = _safe_log_returns(closes)
    if len(r) < 365 + 30:
        return "unknown"
    rolling = np.array([
        float(np.std(r[i - 30:i], ddof=1)) * math.sqrt(365)
        for i in range(30, len(r))
    ])
    p33, p66, p90 = np.percentile(rolling, [33, 66, 90])
    if rv30 < p33:
        return "low"
    if rv30 < p66:
        return "normal"
    if rv30 < p90:
        return "elevated"
    return "extreme"


def moving_average(closes: np.ndarray, window: int) -> Optional[float]:
    if len(closes) < window:
        return None
    return float(np.mean(closes[-window:]))


def mayer_multiple(closes: np.ndarray) -> Optional[float]:
    """Bitcoin-popularised price/200DMA ratio. Generalises fine to any asset."""
    ma200 = moving_average(closes, 200)
    if ma200 is None or ma200 <= 0:
        return None
    return float(closes[-1] / ma200)


def two_hundred_week_ma(closes: np.ndarray) -> Optional[float]:
    """200-week MA — historically the BTC bear-market floor.
    Approximates with 1400 daily bars."""
    return moving_average(closes, 1400)


def cycle_phase(closes: np.ndarray, ticker: str) -> dict:
    """Heuristic cycle phase classification.
    Combines Mayer multiple, 200WMA distance, and drawdown.
    Returns {phase, confidence, mayer, two_hundred_wma_ratio, dd}.
    """
    if len(closes) < 200:
        return {"phase": "warming-up", "confidence": 0.0,
                "mayer": None, "two_hundred_wma_ratio": None, "dd": 0.0}

    mayer = mayer_multiple(closes)
    twma = two_hundred_week_ma(closes)
    twma_ratio = float(closes[-1] / twma) if twma else None
    dd_info = max_drawdown(closes)

    # Phase rules (calibrated on BTC; reasonable on majors).
    # mayer > 2.4 historically marks BTC blow-off tops.
    # closes < 200WMA = capitulation / deep bear.
    phase = "neutral"
    confidence = 0.5
    if mayer is None:
        phase = "warming-up"
        confidence = 0.0
    elif mayer > 2.4:
        phase = "euphoria"
        confidence = min(1.0, (mayer - 2.4) / 0.6 + 0.6)
    elif mayer > 1.5:
        phase = "expansion"
        confidence = min(1.0, (mayer - 1.5) / 0.9 + 0.4)
    elif twma_ratio is not None and twma_ratio < 1.0:
        phase = "capitulation"
        confidence = min(1.0, (1.0 - twma_ratio) + 0.5)
    elif dd_info["current_dd"] < -0.4:
        phase = "deep-bear"
        confidence = min(1.0, abs(dd_info["current_dd"]) - 0.4 + 0.5)
    elif mayer < 1.0:
        phase = "accumulation"
        confidence = 0.6
    return {
        "phase": phase,
        "confidence": round(confidence, 2),
        "mayer": round(mayer, 3) if mayer else None,
        "two_hundred_wma_ratio": round(twma_ratio, 3) if twma_ratio else None,
        "dd": round(dd_info["current_dd"], 3),
    }


def mvrv_proxy(ticker: str) -> Optional[float]:
    """Market cap / realized cap. CoinMetrics gives us both directly for BTC/ETH.
    Returns None for assets without realized-cap coverage."""
    if ticker not in ONCHAIN_COVERED:
        return None
    _, mc = get_onchain_series(ticker, "CapMrktCurUSD", days=2)
    _, rc = get_onchain_series(ticker, "CapRealUSD", days=2)
    if len(mc) == 0 or len(rc) == 0 or rc[-1] <= 0:
        return None
    return float(mc[-1] / rc[-1])


def nvt_ratio(ticker: str) -> Optional[float]:
    """Network value to (adjusted) transfer volume — Woo's NVT.
    Smoothed with a 28-day MA on the denominator to dampen weekend noise."""
    if ticker not in ONCHAIN_COVERED:
        return None
    _, mc = get_onchain_series(ticker, "CapMrktCurUSD", days=2)
    _, tv = get_onchain_series(ticker, "TxTfrValAdjUSD", days=30)
    if len(mc) == 0 or len(tv) < 14:
        return None
    avg_tv = float(np.mean(tv[-28:])) if len(tv) >= 28 else float(np.mean(tv))
    if avg_tv <= 0:
        return None
    return float(mc[-1] / avg_tv)


# ─── DCA recommender ────────────────────────────────────────────────────────

@dataclass
class DCAPlan:
    ticker: str
    base_amount_usd: float
    multiplier: float
    suggested_amount_usd: float
    reason: str
    phase: str
    mayer: Optional[float]
    dd: float
    vol_regime: str

    def to_dict(self) -> dict:
        return asdict(self)


def dca_recommendation(ticker: str, base_amount_usd: float) -> DCAPlan:
    """Cycle-aware DCA suggestion.

    Idea: keep a constant *base* DCA but tilt the multiplier when conditions
    are unusually favourable (deep drawdown, sub-200WMA) or unfavourable
    (Mayer > 2.4 = blow-off territory). The multiplier is bounded [0.0, 2.5]
    so a single bad signal can't blow the user's budget — and importantly,
    `multiplier == 0.0` is allowed: in extreme euphoria we recommend pausing
    new buys rather than fading into a top.
    """
    _, closes = get_daily_closes(ticker, days=365 * 5)
    if len(closes) < 200:
        return DCAPlan(
            ticker=ticker, base_amount_usd=base_amount_usd, multiplier=1.0,
            suggested_amount_usd=base_amount_usd,
            reason="not enough history yet — DCA at base amount",
            phase="warming-up", mayer=None, dd=0.0, vol_regime="unknown",
        )

    cp = cycle_phase(closes, ticker)
    regime = vol_regime(closes)

    multiplier = 1.0
    reasons: list[str] = []

    phase = cp["phase"]
    if phase == "capitulation":
        multiplier *= 2.0
        reasons.append("price below 200WMA — historic accumulation zone")
    elif phase == "deep-bear":
        multiplier *= 1.6
        reasons.append(f"current drawdown {cp['dd']:+.0%}")
    elif phase == "accumulation":
        multiplier *= 1.25
        reasons.append("Mayer < 1 — below 200d average")
    elif phase == "expansion":
        multiplier *= 1.0
        reasons.append("Mayer in expansion range — DCA at base")
    elif phase == "euphoria":
        # Strongly de-risk. > 2.7 = pause new buys.
        m = cp["mayer"] or 2.4
        if m > 2.7:
            multiplier = 0.0
            reasons.append(f"Mayer {m:.2f} — historic top zone, pause buys")
        else:
            multiplier *= 0.5
            reasons.append(f"Mayer {m:.2f} — euphoria, halve buys")

    if regime == "extreme" and phase not in ("euphoria",):
        multiplier *= 1.15
        reasons.append("extreme vol — small fear-buy bonus")
    elif regime == "low" and phase == "expansion":
        multiplier *= 1.05
        reasons.append("low-vol grind — small steady bonus")

    multiplier = max(0.0, min(2.5, multiplier))
    suggested = round(base_amount_usd * multiplier, 2)

    return DCAPlan(
        ticker=ticker, base_amount_usd=base_amount_usd, multiplier=round(multiplier, 2),
        suggested_amount_usd=suggested,
        reason="; ".join(reasons) or "no signal — DCA at base",
        phase=phase, mayer=cp["mayer"], dd=cp["dd"], vol_regime=regime,
    )


# ─── Rebalance recommender ──────────────────────────────────────────────────

@dataclass
class RebalanceLeg:
    ticker: str
    current_weight: float
    target_weight: float
    drift: float
    action: str  # "buy" | "sell" | "hold"
    notional_usd: float

    def to_dict(self) -> dict:
        return asdict(self)


def _latest_price(ticker: str) -> Optional[float]:
    _, closes = get_daily_closes(ticker, days=2)
    return float(closes[-1]) if len(closes) > 0 else None


def rebalance_plan(holdings: list[dict], targets: list[dict],
                   drift_band: float = 0.05) -> dict:
    """Compute rebalance suggestion.

    holdings: [{ticker, qty}]
    targets:  [{ticker, weight}]   weights sum to 1.0
    drift_band: trigger threshold; trades only suggested for legs outside the band.

    Returns {total_usd, legs: [RebalanceLeg], rebalance_required, max_drift}.
    Suggested trades are *closing the gap fully* — clip yourself if you want
    a band-edge rebalance instead.
    """
    prices = {h["ticker"]: _latest_price(h["ticker"]) for h in holdings}
    prices.update({t["ticker"]: prices.get(t["ticker"]) or _latest_price(t["ticker"]) for t in targets})

    values: dict[str, float] = {}
    for h in holdings:
        p = prices.get(h["ticker"])
        if p is None:
            continue
        values[h["ticker"]] = float(h["qty"]) * p
    total = sum(values.values())
    if total <= 0:
        return {"total_usd": 0.0, "legs": [], "rebalance_required": False, "max_drift": 0.0}

    target_map = {t["ticker"]: float(t["weight"]) for t in targets}
    legs: list[RebalanceLeg] = []
    max_drift = 0.0
    for ticker, tgt_w in target_map.items():
        cur_v = values.get(ticker, 0.0)
        cur_w = cur_v / total
        drift = cur_w - tgt_w
        max_drift = max(max_drift, abs(drift))
        if abs(drift) <= drift_band:
            action = "hold"
            notional = 0.0
        elif drift > 0:
            action = "sell"
            notional = round(drift * total, 2)
        else:
            action = "buy"
            notional = round(abs(drift) * total, 2)
        legs.append(RebalanceLeg(
            ticker=ticker, current_weight=round(cur_w, 4),
            target_weight=round(tgt_w, 4), drift=round(drift, 4),
            action=action, notional_usd=notional,
        ))

    # Tickers in holdings but not in targets — flag as full-sell candidates
    for ticker, v in values.items():
        if ticker in target_map:
            continue
        cur_w = v / total
        legs.append(RebalanceLeg(
            ticker=ticker, current_weight=round(cur_w, 4), target_weight=0.0,
            drift=round(cur_w, 4), action="sell" if cur_w > drift_band else "hold",
            notional_usd=round(v, 2) if cur_w > drift_band else 0.0,
        ))
        max_drift = max(max_drift, cur_w)

    return {
        "total_usd": round(total, 2),
        "legs": [leg.to_dict() for leg in legs],
        "rebalance_required": max_drift > drift_band,
        "max_drift": round(max_drift, 4),
    }


# ─── Risk-off composite signal ──────────────────────────────────────────────

def risk_off_signal(ticker: str) -> dict:
    """Composite score [0, 1] — higher = more reasons to be defensive.

    Components (each 0..1 then averaged):
      - Mayer pressure (Mayer over 2.4 is bad)
      - Drawdown stress (deeper = higher, but only the downward leg)
      - Vol regime (extreme/elevated push score up)
      - MVRV stress (BTC/ETH only; > 3.5 historically marked tops)
    Components missing data are dropped from the average.
    """
    _, closes = get_daily_closes(ticker, days=400)
    if len(closes) < 30:
        return {"score": 0.0, "components": {}, "label": "insufficient-data"}

    cp = cycle_phase(closes, ticker)
    regime = vol_regime(closes)
    components: dict[str, float] = {}

    if cp["mayer"] is not None:
        # 1.0 at Mayer 1.0; rises sharply above 2.0.
        m = cp["mayer"]
        if m <= 1.0:
            components["mayer"] = 0.0
        else:
            components["mayer"] = min(1.0, (m - 1.0) / 1.6)

    # Drawdown is *protection* on the way down, not risk-off — so we only mark
    # it as risk-off when price is pinned to the highs (current_dd close to 0).
    components["near-highs"] = 1.0 - min(1.0, abs(cp["dd"]) / 0.2)

    components["vol"] = {"low": 0.0, "normal": 0.2, "elevated": 0.6, "extreme": 0.9}.get(regime, 0.0)

    mvrv = mvrv_proxy(ticker)
    if mvrv is not None:
        # 0 at MVRV 1.0, 1.0 at MVRV 3.5+.
        components["mvrv"] = max(0.0, min(1.0, (mvrv - 1.0) / 2.5))

    score = float(np.mean(list(components.values()))) if components else 0.0
    label = "calm"
    if score >= 0.7:
        label = "defensive"
    elif score >= 0.45:
        label = "watchful"
    elif score >= 0.25:
        label = "neutral"
    return {"score": round(score, 3), "components": {k: round(v, 3) for k, v in components.items()}, "label": label}


# ─── Snapshot for the dashboard ─────────────────────────────────────────────

def asset_snapshot(ticker: str) -> dict:
    """Everything the long-term tab needs in one shot."""
    _, closes = get_daily_closes(ticker, days=365 * 4)
    if len(closes) == 0:
        return {"ticker": ticker, "ready": False}

    cp = cycle_phase(closes, ticker)
    dd = max_drawdown(closes)
    snapshot = {
        "ticker": ticker,
        "ready": True,
        "price": round(float(closes[-1]), 6),
        "ma50": round(moving_average(closes, 50) or 0, 4),
        "ma200": round(moving_average(closes, 200) or 0, 4),
        "ma_200w": round(two_hundred_week_ma(closes) or 0, 4),
        "mayer": cp["mayer"],
        "phase": cp["phase"],
        "phase_confidence": cp["confidence"],
        "current_dd": dd["current_dd"],
        "max_dd": dd["max_dd"],
        "vol_30d": round(realized_volatility(closes, 30, True), 4),
        "vol_regime": vol_regime(closes),
        "sharpe_1y": round(sharpe_ratio(closes[-365:]) if len(closes) >= 365 else float("nan"), 3),
        "sortino_1y": round(sortino_ratio(closes[-365:]) if len(closes) >= 365 else float("nan"), 3),
        "mvrv": mvrv_proxy(ticker),
        "nvt": nvt_ratio(ticker),
        "risk_off": risk_off_signal(ticker),
        "history_days": len(closes),
        "last_bar_date": db.get_latest_daily_bar_date(ticker),
    }
    # Some metrics are NaN before they have enough data — make those JSON-safe.
    for k, v in list(snapshot.items()):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            snapshot[k] = None
    return snapshot


def all_snapshots() -> list[dict]:
    return [asset_snapshot(t) for t in TICKER_MAP.keys()]


# ─── Refresh job ────────────────────────────────────────────────────────────

def refresh_all(days: int = HISTORY_DAYS_DEFAULT) -> dict:
    """Pull fresh daily bars + on-chain metrics for every ticker.
    Intended to be called once on startup and then on a slow timer (every 6h).
    """
    started = time.time()
    out = {"bars": {}, "onchain": {}, "elapsed_s": 0.0}
    for ticker in TICKER_MAP.keys():
        try:
            out["bars"][ticker] = refresh_daily_bars(ticker, days=days)
        except Exception as e:
            log.warning("daily bar refresh failed for %s: %s", ticker, e)
            out["bars"][ticker] = -1
        try:
            out["onchain"][ticker] = refresh_onchain_metrics(ticker, days=days)
        except Exception as e:
            log.warning("onchain refresh failed for %s: %s", ticker, e)
            out["onchain"][ticker] = -1
    out["elapsed_s"] = round(time.time() - started, 2)
    return out
