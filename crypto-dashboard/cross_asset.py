#!/usr/bin/env python3
"""
Cross-asset overlay — BTC vs crypto-equities vs broad-market vs safe-haven.

Bloomberg's killer feature for HODLers: side-by-side comparison of BTC
with the assets that move it (or that should). "Is BTC outperforming MSTR
right now?" is a question every crypto-equity holder asks weekly; here
it's a single chart.

Data source: Stooq daily bars (same path macro.py + etf_flows.py use).
Free, no auth, reliable. US equities use `<ticker>.us`, FX uses `^<sym>`.

Assets tracked:
  - BTC (reference series — pulled via the spot path, not Stooq)
  - MSTR (Strategy Inc — corporate BTC treasury proxy)
  - COIN (Coinbase Global — crypto-exchange equity)
  - RIOT, MARA, CLSK (BTC miners)
  - SPY (S&P 500 — broad market)
  - QQQ (Nasdaq 100 — tech beta)
  - GLD (gold — alternative store of value)
  - TLT (20Y Treasuries — duration / safe-haven)

Math:
  - **Normalised series**: rebase each asset to 100 at the window start
    so absolute price levels don't dominate the chart.
  - **Rolling correlation**: Pearson on daily log returns over the
    window. Aligned on business days (equity calendar) — BTC trades 7
    days/week, equities don't, and that mismatch otherwise inflates
    short-window correlations.
  - **Beta vs BTC**: cov(asset_returns, btc_returns) / var(btc_returns).
    Tells you how much each asset moves per 1% BTC move (MSTR is ~2.5x,
    RIOT/MARA ~3x, SPY ~0.2x).
  - **Period returns**: 7d / 30d / YTD for the comparison table.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import requests

import database as db
import long_term as lt

log = logging.getLogger("crypto.cross_asset")

STOOQ_BASE = "https://stooq.com/q/d/l/"
USER_AGENT = "CryptoEdge-CrossAssetBot/1.0 (https://crypto.narve.ai)"


# ─── Registry ───────────────────────────────────────────────────────────────

# (ticker, name, category, stooq_code). category is used by the UI for
# grouping (crypto / crypto-equity / broad-market / safe-haven).
CROSS_ASSET_REGISTRY = {
    "MSTR": {"name": "Strategy Inc",    "category": "crypto-equity", "stooq": "mstr.us"},
    "COIN": {"name": "Coinbase Global", "category": "crypto-equity", "stooq": "coin.us"},
    "RIOT": {"name": "Riot Platforms",  "category": "miner",         "stooq": "riot.us"},
    "MARA": {"name": "MARA Holdings",   "category": "miner",         "stooq": "mara.us"},
    "CLSK": {"name": "CleanSpark",      "category": "miner",         "stooq": "clsk.us"},
    "SPY":  {"name": "S&P 500",         "category": "broad-market",  "stooq": "spy.us"},
    "QQQ":  {"name": "Nasdaq 100",      "category": "broad-market",  "stooq": "qqq.us"},
    "GLD":  {"name": "Gold",            "category": "safe-haven",    "stooq": "gld.us"},
    "TLT":  {"name": "20Y Treasuries",  "category": "safe-haven",    "stooq": "tlt.us"},
}

# Categories in display order.
CATEGORY_ORDER = ["crypto-equity", "miner", "broad-market", "safe-haven"]


# ─── Stooq fetch ────────────────────────────────────────────────────────────

def _fetch_stooq(code: str, years: int = 2) -> list[tuple[str, float, float, float, float, float]]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=365 * years)
    try:
        r = requests.get(
            STOOQ_BASE,
            params={"s": code, "d1": start.strftime("%Y%m%d"),
                    "d2": end.strftime("%Y%m%d"), "i": "d"},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code >= 400 or not r.text:
            return []
    except requests.RequestException as e:
        log.warning("stooq fetch failed for %s: %s", code, e)
        return []
    out = []
    reader = csv.DictReader(io.StringIO(r.text))
    for row in reader:
        try:
            d = row.get("Date") or row.get("date")
            o = float(row.get("Open") or 0)
            h = float(row.get("High") or 0)
            lo = float(row.get("Low") or 0)
            c = float(row.get("Close") or 0)
            v = float(row.get("Volume") or 0)
            if not d or c <= 0:
                continue
            out.append((d, o, h, lo, c, v))
        except (TypeError, ValueError):
            continue
    return out


def refresh() -> dict:
    """Pull daily bars for every cross-asset ticker. Idempotent."""
    started = time.time()
    summary: dict = {"tickers": {}, "elapsed_s": 0.0}
    total_new = 0
    for ticker, meta in CROSS_ASSET_REGISTRY.items():
        try:
            rows_raw = _fetch_stooq(meta["stooq"], years=2)
        except Exception as e:
            log.warning("cross-asset refresh failed for %s: %s", ticker, e)
            summary["tickers"][ticker] = -1
            continue
        rows = [(ticker, *r) for r in rows_raw]
        result = db.upsert_cross_asset_bars(rows) if rows else {"new": 0}
        summary["tickers"][ticker] = result.get("new", 0)
        total_new += result.get("new", 0)
    summary["new"] = total_new
    summary["elapsed_s"] = round(time.time() - started, 2)
    return summary


# ─── Series helpers ─────────────────────────────────────────────────────────

def _get_series(ticker: str, days: int) -> tuple[list[str], np.ndarray]:
    """Return (iso_dates, closes) for the asset over the trailing window.
    BTC pulls from the spot daily-bar table; everything else from
    cross-asset bars."""
    if ticker == "BTC":
        return lt.get_daily_closes("BTC", days=days)
    rows = db.get_cross_asset_bars(ticker, days=days)
    dates = [r["date"] for r in rows]
    closes = np.asarray([r["close"] for r in rows], dtype=np.float64)
    return dates, closes


def _aligned_pair(t1: str, t2: str, days: int) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Get returns for both assets aligned on the dates they have in common
    (typically equity business days, since BTC trades 7d/wk but the
    equities don't)."""
    d1, c1 = _get_series(t1, days)
    d2, c2 = _get_series(t2, days)
    if len(c1) == 0 or len(c2) == 0:
        return np.array([]), np.array([]), []
    # Inner-join on dates.
    m1 = {d: i for i, d in enumerate(d1)}
    common = [d for d in d2 if d in m1]
    if len(common) < 3:
        return np.array([]), np.array([]), []
    common.sort()
    a1 = np.asarray([c1[m1[d]] for d in common], dtype=np.float64)
    a2 = np.asarray([c2[d2.index(d)] for d in common], dtype=np.float64)
    return a1, a2, common


# ─── Analytics ──────────────────────────────────────────────────────────────

def _returns(closes: np.ndarray) -> np.ndarray:
    if len(closes) < 2:
        return np.array([])
    return np.diff(np.log(closes))


def _period_return(closes: np.ndarray, n: int) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    if closes[-(n + 1)] <= 0:
        return None
    return float(closes[-1] / closes[-(n + 1)] - 1.0)


def _ytd_return(dates: list[str], closes: np.ndarray) -> Optional[float]:
    """Return YTD performance — finds the first trading day of the current
    year and measures from there."""
    if len(closes) == 0:
        return None
    year = datetime.now(timezone.utc).year
    target = f"{year}-01-01"
    # First date >= target
    idx = None
    for i, d in enumerate(dates):
        if d >= target:
            idx = i
            break
    if idx is None or idx >= len(closes) - 1:
        return None
    if closes[idx] <= 0:
        return None
    return float(closes[-1] / closes[idx] - 1.0)


def _correlation(t1: str, t2: str, window_days: int) -> Optional[float]:
    """Pearson correlation of daily log returns over `window_days` calendar
    days. After inner-join on equity-business-day dates a 30-calendar-day
    window yields ~22 aligned bars, so we use a percentage threshold of
    the requested window (60%) rather than a hardcoded 30 — that way a
    30d window needs ~18 observations and a 90d window needs ~54."""
    a1, a2, _ = _aligned_pair(t1, t2, days=window_days)
    min_obs = max(10, int(window_days * 0.6))
    if len(a1) < min_obs or len(a2) < min_obs:
        return None
    r1 = _returns(a1)
    r2 = _returns(a2)
    if len(r1) < min_obs - 1 or len(r2) < min_obs - 1:
        return None
    if np.std(r1) == 0 or np.std(r2) == 0:
        return None
    return float(np.corrcoef(r1, r2)[0, 1])


def _beta(asset: str, ref: str, window_days: int) -> Optional[float]:
    """Beta of `asset` returns vs `ref` returns. > 1 = more volatile.
    Same percentage-threshold approach as _correlation so 30d windows
    aren't unconditionally rejected after business-day inner-join."""
    a, b, _ = _aligned_pair(asset, ref, days=window_days)
    min_obs = max(10, int(window_days * 0.6))
    if len(a) < min_obs or len(b) < min_obs:
        return None
    ra = _returns(a)
    rb = _returns(b)
    n = min(len(ra), len(rb))
    if n < min_obs - 1:
        return None
    ra, rb = ra[-n:], rb[-n:]
    var_ref = float(np.var(rb))
    if var_ref == 0:
        return None
    return float(np.cov(ra, rb)[0, 1] / var_ref)


# ─── Snapshot for the UI ────────────────────────────────────────────────────

@dataclass
class CrossAssetRow:
    ticker: str
    name: str
    category: str
    last_close: float
    last_date: str
    return_7d: Optional[float]
    return_30d: Optional[float]
    return_ytd: Optional[float]
    corr_btc_30d: Optional[float]
    corr_btc_90d: Optional[float]
    beta_btc_90d: Optional[float]

    def to_dict(self) -> dict:
        return asdict(self)


def overview() -> dict:
    """One row per tracked asset + BTC. Returns + correlations + betas."""
    rows: list[CrossAssetRow] = []
    # BTC reference row first (so the UI can use it as the anchor).
    for ticker in ["BTC"] + list(CROSS_ASSET_REGISTRY.keys()):
        if ticker == "BTC":
            meta = {"name": "Bitcoin", "category": "crypto"}
        else:
            meta = CROSS_ASSET_REGISTRY[ticker]
        dates, closes = _get_series(ticker, days=400)
        if len(closes) == 0:
            continue
        rows.append(CrossAssetRow(
            ticker=ticker, name=meta["name"], category=meta["category"],
            last_close=round(float(closes[-1]), 4),
            last_date=dates[-1] if dates else "",
            return_7d=_period_return(closes, 7),
            return_30d=_period_return(closes, 30),
            return_ytd=_ytd_return(dates, closes),
            corr_btc_30d=_correlation(ticker, "BTC", 30) if ticker != "BTC" else 1.0,
            corr_btc_90d=_correlation(ticker, "BTC", 90) if ticker != "BTC" else 1.0,
            beta_btc_90d=_beta(ticker, "BTC", 90) if ticker != "BTC" else 1.0,
        ))
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "rows": [r.to_dict() for r in rows],
        "category_order": ["crypto"] + CATEGORY_ORDER,
    }


def normalised_series(tickers: list[str], days: int = 90) -> dict:
    """Each ticker's series rebased to 100 at the window start. The UI
    overlays them on a single chart so users can see relative
    performance over time. Inner-joins on common dates so the lines
    don't drift apart on weekends/holidays."""
    out: dict = {"as_of": datetime.now(timezone.utc).isoformat(), "series": {}}
    if not tickers:
        return out
    # Find the union of all date sets first.
    raw = {}
    for t in tickers:
        d, c = _get_series(t, days=days)
        if len(c) > 0:
            raw[t] = (d, c)
    if not raw:
        return out
    # Inner-join on dates so the chart x-axis is consistent.
    common = None
    for t, (d, _) in raw.items():
        s = set(d)
        common = s if common is None else (common & s)
    if not common:
        return out
    common_sorted = sorted(common)
    if len(common_sorted) < 2:
        return out
    out["dates"] = common_sorted
    for t, (d, c) in raw.items():
        idx_map = {date: i for i, date in enumerate(d)}
        aligned = np.asarray([c[idx_map[date]] for date in common_sorted],
                              dtype=np.float64)
        if aligned[0] <= 0:
            continue
        # Rebase to 100.
        normalised = aligned / aligned[0] * 100.0
        out["series"][t] = [round(float(v), 2) for v in normalised]
    return out
