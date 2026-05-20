#!/usr/bin/env python3
"""
Derivatives data layer — funding rates, open interest, perp basis.

Pulls from Binance USDT-M Futures (free, no auth). Could be extended to
Bybit/OKX in future; data shapes are similar.

Why this matters for HODLers:
  - **Funding rate** is the single best risk-off signal in crypto. Sustained
    funding > +0.05% / 8h (= ~55%/yr) = overheated longs. Sustained < -0.02% / 8h
    = capitulation, historic dip-buy zone.
  - **Open interest** confirms moves. Price up + OI up = real money flowing in.
    Price up + OI down = short squeeze (less durable).
  - **Perp basis** (perp price vs spot) tracks sentiment. Wide positive basis =
    leverage stacking long.

The module persists time series so we can compute percentile ranks and
historical context, not just point-in-time values.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import requests

import database as db
import long_term as lt

log = logging.getLogger("crypto.derivatives")

# USDT-M perpetuals — only the assets we already track and that Binance offers.
BINANCE_FUTURES_BASE = "https://fapi.binance.com"
PERP_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "DOGE": "DOGEUSDT", "XRP": "XRPUSDT",
}


# ─── Fetchers ───────────────────────────────────────────────────────────────

def _safe_get(url: str, params: dict | None = None, timeout: int = 15):
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == 2:
                log.warning("GET %s failed: %s", url, e)
                return None
            time.sleep(1 + attempt)
    return None


def fetch_premium_index(ticker: str) -> Optional[dict]:
    """Returns current mark price, index price, last funding rate, and next
    funding time for one symbol."""
    sym = PERP_SYMBOLS.get(ticker)
    if not sym:
        return None
    return _safe_get(f"{BINANCE_FUTURES_BASE}/fapi/v1/premiumIndex", {"symbol": sym})


def fetch_funding_history(ticker: str, limit: int = 1000) -> list[dict]:
    """Past funding rates (one per 8h). Limit 1000 = ~333 days."""
    sym = PERP_SYMBOLS.get(ticker)
    if not sym:
        return []
    out = _safe_get(f"{BINANCE_FUTURES_BASE}/fapi/v1/fundingRate",
                    {"symbol": sym, "limit": limit})
    return out or []


def fetch_open_interest_hist(ticker: str, period: str = "1d", limit: int = 500) -> list[dict]:
    """Historical open interest (last 30d max for 1h period, ~500d for 1d)."""
    sym = PERP_SYMBOLS.get(ticker)
    if not sym:
        return []
    out = _safe_get(f"{BINANCE_FUTURES_BASE}/futures/data/openInterestHist",
                    {"symbol": sym, "period": period, "limit": limit})
    return out or []


def fetch_taker_buysell_ratio(ticker: str, period: str = "1d", limit: int = 30) -> list[dict]:
    """Aggregated taker buy/sell volume ratio. >1 = aggressive buying."""
    sym = PERP_SYMBOLS.get(ticker)
    if not sym:
        return []
    out = _safe_get(f"{BINANCE_FUTURES_BASE}/futures/data/takerlongshortRatio",
                    {"symbol": sym, "period": period, "limit": limit})
    return out or []


# ─── Refresh job ────────────────────────────────────────────────────────────

def refresh_funding(ticker: str) -> int:
    """Pull recent funding rates and upsert. Returns rows inserted."""
    rows_raw = fetch_funding_history(ticker, limit=1000)
    rows = []
    for r in rows_raw:
        try:
            t_ms = int(r["fundingTime"])
            ts = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).isoformat()
            rows.append((ticker, ts, float(r["fundingRate"]), "funding_rate"))
        except (KeyError, TypeError, ValueError):
            continue
    if rows:
        db.upsert_derivatives_series(rows)
    return len(rows)


def refresh_open_interest(ticker: str) -> int:
    rows_raw = fetch_open_interest_hist(ticker, period="1d", limit=500)
    rows = []
    for r in rows_raw:
        try:
            t_ms = int(r["timestamp"])
            ts = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).isoformat()
            # `sumOpenInterestValue` is OI in USDT — easier to compare across assets.
            rows.append((ticker, ts, float(r["sumOpenInterestValue"]), "open_interest_usd"))
        except (KeyError, TypeError, ValueError):
            continue
    if rows:
        db.upsert_derivatives_series(rows)
    return len(rows)


def refresh_basis(ticker: str) -> int:
    """Snapshot the current perp vs index basis. Stored as a point sample."""
    pi = fetch_premium_index(ticker)
    if not pi:
        return 0
    try:
        mark = float(pi["markPrice"])
        index = float(pi["indexPrice"])
    except (KeyError, TypeError, ValueError):
        return 0
    if index <= 0:
        return 0
    basis = (mark - index) / index
    ts = datetime.now(timezone.utc).isoformat()
    db.upsert_derivatives_series([(ticker, ts, basis, "perp_basis")])
    return 1


def refresh_all_derivatives() -> dict:
    """One sweep of all tickers, all series. Safe to call hourly."""
    started = time.time()
    out = {"funding": {}, "oi": {}, "basis": {}, "elapsed_s": 0.0}
    for ticker in PERP_SYMBOLS:
        try:
            out["funding"][ticker] = refresh_funding(ticker)
        except Exception as e:
            log.warning("funding refresh failed for %s: %s", ticker, e)
            out["funding"][ticker] = -1
        try:
            out["oi"][ticker] = refresh_open_interest(ticker)
        except Exception as e:
            log.warning("oi refresh failed for %s: %s", ticker, e)
            out["oi"][ticker] = -1
        try:
            out["basis"][ticker] = refresh_basis(ticker)
        except Exception as e:
            log.warning("basis refresh failed for %s: %s", ticker, e)
            out["basis"][ticker] = -1
    out["elapsed_s"] = round(time.time() - started, 2)
    return out


# ─── Analytics ──────────────────────────────────────────────────────────────

@dataclass
class FundingSnapshot:
    ticker: str
    current_rate: float           # last 8h funding
    annualised: float             # current × 3 × 365
    avg_7d: float                 # mean of last 21 rates
    avg_30d: float
    percentile_rank_1y: Optional[float]
    signal: str                   # bullish | neutral | bearish
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


def funding_snapshot(ticker: str) -> Optional[FundingSnapshot]:
    """Read the funding history from DB and compute the snapshot."""
    rows = db.get_derivatives_series(ticker, "funding_rate", days=365)
    if not rows:
        return None
    vals = np.asarray([r["value"] for r in rows], dtype=np.float64)
    last = float(vals[-1])
    annualised = last * 3 * 365
    avg7 = float(np.mean(vals[-21:])) if len(vals) >= 21 else last
    avg30 = float(np.mean(vals[-90:])) if len(vals) >= 90 else avg7
    sorted_vals = np.sort(vals)
    rank = float(np.searchsorted(sorted_vals, last) / len(sorted_vals))

    if avg7 > 0.0005:
        signal = "bearish"
        desc = f"7d avg funding {avg7*100:.4f}%/8h — heavy long crowding"
    elif avg7 < -0.0002:
        signal = "bullish"
        desc = f"7d avg funding {avg7*100:.4f}%/8h — shorts paying, capitulation zone"
    else:
        signal = "neutral"
        desc = f"7d avg funding {avg7*100:.4f}%/8h (normal)"

    return FundingSnapshot(
        ticker=ticker, current_rate=last, annualised=annualised,
        avg_7d=avg7, avg_30d=avg30, percentile_rank_1y=rank,
        signal=signal, description=desc,
    )


@dataclass
class OISnapshot:
    ticker: str
    current_usd: float
    pct_change_7d: Optional[float]
    pct_change_30d: Optional[float]
    signal: str
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


def oi_snapshot(ticker: str) -> Optional[OISnapshot]:
    rows = db.get_derivatives_series(ticker, "open_interest_usd", days=90)
    if not rows:
        return None
    vals = np.asarray([r["value"] for r in rows], dtype=np.float64)
    cur = float(vals[-1])
    c7 = (cur / vals[-8] - 1.0) if len(vals) >= 8 and vals[-8] > 0 else None
    c30 = (cur / vals[-30] - 1.0) if len(vals) >= 30 and vals[-30] > 0 else None
    # Need price to know if OI is moving with or against price.
    _, closes = lt.get_daily_closes(ticker, days=10)
    p7 = (float(closes[-1]) / float(closes[-8]) - 1.0) if len(closes) >= 8 else None
    if c7 is not None and p7 is not None:
        if p7 > 0 and c7 > 0:
            signal, desc = "neutral", "Price↑ + OI↑ — real money, healthy trend"
        elif p7 < 0 and c7 > 0:
            signal, desc = "bearish", "Price↓ + OI↑ — shorts piling on, leverage building"
        elif p7 > 0 and c7 < 0:
            signal, desc = "bullish", "Price↑ + OI↓ — short squeeze, less durable"
        else:
            signal, desc = "bullish", "Price↓ + OI↓ — leverage flushing, cleansing"
    else:
        signal, desc = "neutral", "insufficient data for trend"
    return OISnapshot(
        ticker=ticker, current_usd=cur,
        pct_change_7d=round(c7, 4) if c7 is not None else None,
        pct_change_30d=round(c30, 4) if c30 is not None else None,
        signal=signal, description=desc,
    )


def funding_composite() -> dict:
    """Aggregate funding signal across BTC+ETH (the liquid majors).
    Returns {score: -1..+1, label, components}.
    +1 = extreme long crowding (risk-off); -1 = extreme capitulation (risk-on)."""
    components = []
    for t in ("BTC", "ETH"):
        snap = funding_snapshot(t)
        if not snap:
            continue
        # Map the 7d-avg into [-1, +1] with calibrated bounds.
        # +0.001 / 8h = +110%/yr saturates at +1.
        # -0.0005 / 8h = -55%/yr saturates at -1.
        val = snap.avg_7d
        if val >= 0:
            s = min(1.0, val / 0.001)
        else:
            s = max(-1.0, val / 0.0005)
        components.append({"ticker": t, "avg_7d": round(val, 6), "score": round(s, 3)})
    if not components:
        return {"score": None, "label": "no-data", "components": []}
    avg = float(np.mean([c["score"] for c in components]))
    if avg > 0.5:
        label = "long-crowding"
    elif avg > 0.15:
        label = "lean-long"
    elif avg > -0.15:
        label = "balanced"
    elif avg > -0.5:
        label = "lean-short"
    else:
        label = "capitulation"
    return {"score": round(avg, 3), "label": label, "components": components}
