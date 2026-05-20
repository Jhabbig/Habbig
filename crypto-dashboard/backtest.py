#!/usr/bin/env python3
"""
Walk-forward backtester for cycle/on-chain indicators.

For each indicator, we test the hypothesis: *if I had only bought (or DCAd
more) when this indicator signalled bullish, would I have outperformed
buying-on-the-dates-where-it-signalled-anything?*

Crucially **walk-forward**: at each historical date, we recompute the
indicator using only data available up to that date, then look forward
30/90/365 days to measure the outcome. No peeking.

Results persist in `crypto_indicator_backtests` keyed by (indicator, ticker,
horizon_days, computed_at). The UI shows the most recent rollup; you can
rerun on demand.

Limitations to be honest about:
  - We only have 4y of daily history once the refresh job has run a full
    backfill. That covers one halving cycle. Results from a single cycle
    are not statistically robust — treat them as illustrative.
  - Some indicators (e.g. exchange flows) only have data once we start
    storing it; their backtest sample sizes will grow over time.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

import numpy as np

import database as db
import long_term as lt
import indicators as ind

log = logging.getLogger("crypto.backtest")


@dataclass
class BacktestResult:
    indicator: str
    ticker: str
    horizon_days: int
    fired_n: int                      # number of times the signal fired
    median_fwd_return: Optional[float]
    mean_fwd_return: Optional[float]
    win_rate: Optional[float]         # share of fires with positive forward return
    median_baseline_return: float     # what holding any random day produced
    median_excess: Optional[float]    # median fwd - baseline
    hit_ratio: Optional[float]        # fwd_return > baseline (per-fire)
    sample_window_days: int
    computed_at: str

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Walk-forward signal computation ────────────────────────────────────────

def _walk_forward_signals(ticker: str, indicator_name: str) -> list[tuple[str, str]]:
    """Recompute one indicator across all historical dates.
    Returns [(date_iso, signal)] where signal ∈ {bullish, neutral, bearish, unavailable}.

    Strategy: we don't re-run the indicator with truncated input (slow + the
    indicator functions don't take a 'now' argument). Instead, for each
    indicator we encode a *pure function from a price/onchain window* directly
    here. Slower to maintain but it preserves walk-forward correctness.
    """
    _, closes = lt.get_daily_closes(ticker, days=365 * 5)
    if len(closes) < 200:
        return []

    n = len(closes)
    signals: list[tuple[str, str]] = []
    today = datetime.now(timezone.utc).date()
    first_offset = max(0, n - 365 * 4)  # only test on the last 4y

    if indicator_name == "pi_cycle_top":
        for i in range(max(first_offset, 350), n):
            window = closes[:i + 1]
            ma111 = float(np.mean(window[-111:])) if len(window) >= 111 else None
            ma350 = float(np.mean(window[-350:])) if len(window) >= 350 else None
            if not ma111 or not ma350 or ma350 == 0:
                continue
            ratio = (ma111 * 2) / ma350
            sig = "bearish" if ratio >= 0.95 else "bullish" if ratio < 0.5 else "neutral"
            date_iso = (today - timedelta(days=(n - 1 - i))).isoformat()
            signals.append((date_iso, sig))

    elif indicator_name == "two_hundred_week_distance":
        for i in range(max(first_offset, 1400), n):
            window = closes[:i + 1]
            twma = float(np.mean(window[-1400:]))
            if twma <= 0:
                continue
            ratio = float(window[-1] / twma)
            sig = "bullish" if ratio < 1.0 else "bearish" if ratio > 3.0 else "neutral"
            date_iso = (today - timedelta(days=(n - 1 - i))).isoformat()
            signals.append((date_iso, sig))

    elif indicator_name == "mayer":
        for i in range(max(first_offset, 200), n):
            window = closes[:i + 1]
            ma200 = float(np.mean(window[-200:]))
            if ma200 <= 0:
                continue
            mayer = float(window[-1] / ma200)
            if mayer < 1.0:
                sig = "bullish"
            elif mayer > 2.4:
                sig = "bearish"
            else:
                sig = "neutral"
            date_iso = (today - timedelta(days=(n - 1 - i))).isoformat()
            signals.append((date_iso, sig))

    elif indicator_name == "drawdown":
        for i in range(max(first_offset, 30), n):
            window = closes[:i + 1]
            peak = float(np.maximum.accumulate(window)[-1])
            dd = float((window[-1] - peak) / peak) if peak > 0 else 0
            if dd <= -0.4:
                sig = "bullish"
            elif dd >= -0.05:
                sig = "bearish"
            else:
                sig = "neutral"
            date_iso = (today - timedelta(days=(n - 1 - i))).isoformat()
            signals.append((date_iso, sig))

    elif indicator_name == "nupl":
        if ticker not in lt.ONCHAIN_COVERED:
            return []
        oc_mc = db.get_onchain_metric(ticker, "CapMrktCurUSD", days=365 * 5)
        oc_rc = db.get_onchain_metric(ticker, "CapRealUSD", days=365 * 5)
        mc_by_date = {r["date"]: r["value"] for r in oc_mc}
        rc_by_date = {r["date"]: r["value"] for r in oc_rc}
        for i in range(first_offset, n):
            d = (today - timedelta(days=(n - 1 - i))).isoformat()
            mc, rc = mc_by_date.get(d), rc_by_date.get(d)
            if mc is None or rc is None or mc <= 0:
                continue
            v = (mc - rc) / mc
            if v < 0.25:
                sig = "bullish"
            elif v > 0.75:
                sig = "bearish"
            else:
                sig = "neutral"
            signals.append((d, sig))

    elif indicator_name == "puell_multiple":
        if ticker not in ("BTC", "ETH"):
            return []
        oc_iss = db.get_onchain_metric(ticker, "IssTotNtv", days=365 * 5)
        iss_by_date = {r["date"]: r["value"] for r in oc_iss}
        # Build USD issuance series aligned to closes.
        iss_usd = []
        for i in range(n):
            d = (today - timedelta(days=(n - 1 - i))).isoformat()
            ntv = iss_by_date.get(d)
            if ntv is None and ticker == "BTC":
                ntv = ind._btc_block_reward_at(
                    datetime.now(timezone.utc) - timedelta(days=(n - 1 - i))) * 144
            iss_usd.append(ntv * closes[i] if ntv is not None else None)
        for i in range(max(first_offset, 366), n):
            window = [v for v in iss_usd[i - 365:i] if v is not None]
            if len(window) < 200 or iss_usd[i] is None:
                continue
            ma365 = sum(window) / len(window)
            if ma365 <= 0:
                continue
            val = iss_usd[i] / ma365
            if val < 0.5:
                sig = "bullish"
            elif val > 4.0:
                sig = "bearish"
            else:
                sig = "neutral"
            d = (today - timedelta(days=(n - 1 - i))).isoformat()
            signals.append((d, sig))

    return signals


# ─── Forward-return computation ─────────────────────────────────────────────

def _forward_returns(ticker: str, signal_dates: list[str], horizon_days: int) -> dict:
    """For each (date, signal) pair, compute the price return horizon_days later.
    Returns the price by date map plus aligned returns by signal class."""
    rows = db.get_daily_bars(ticker, days=365 * 5)
    close_by_date = {r["date"]: r["close"] for r in rows}
    sorted_dates = sorted(close_by_date.keys())
    date_to_idx = {d: i for i, d in enumerate(sorted_dates)}

    bullish_returns: list[float] = []
    bearish_returns: list[float] = []
    neutral_returns: list[float] = []
    baseline_returns: list[float] = []

    for d, sig in signal_dates:
        if d not in date_to_idx:
            continue
        i = date_to_idx[d]
        if i + horizon_days >= len(sorted_dates):
            continue
        p0 = close_by_date[sorted_dates[i]]
        p1 = close_by_date[sorted_dates[i + horizon_days]]
        if p0 <= 0:
            continue
        r = (p1 / p0 - 1.0)
        if sig == "bullish":
            bullish_returns.append(r)
        elif sig == "bearish":
            bearish_returns.append(r)
        elif sig == "neutral":
            neutral_returns.append(r)
        baseline_returns.append(r)

    return {
        "bullish": bullish_returns,
        "bearish": bearish_returns,
        "neutral": neutral_returns,
        "baseline": baseline_returns,
    }


# ─── Top-level runner ───────────────────────────────────────────────────────

INDICATORS_TO_BACKTEST = [
    "pi_cycle_top",
    "two_hundred_week_distance",
    "mayer",
    "drawdown",
    "nupl",
    "puell_multiple",
]

HORIZONS = [30, 90, 365]


def backtest_indicator(indicator: str, ticker: str, horizon: int) -> Optional[BacktestResult]:
    sigs = _walk_forward_signals(ticker, indicator)
    if not sigs:
        return None
    returns = _forward_returns(ticker, sigs, horizon)
    bull = returns["bullish"]
    baseline = returns["baseline"]
    if len(baseline) < 30:
        return None
    baseline_arr = np.asarray(baseline)
    median_baseline = float(np.median(baseline_arr))

    if bull:
        bull_arr = np.asarray(bull)
        median_bull = float(np.median(bull_arr))
        mean_bull = float(np.mean(bull_arr))
        win_rate = float(np.mean(bull_arr > 0))
        excess = median_bull - median_baseline
        hit_ratio = float(np.mean(bull_arr > median_baseline))
    else:
        median_bull = mean_bull = win_rate = excess = hit_ratio = None

    return BacktestResult(
        indicator=indicator, ticker=ticker, horizon_days=horizon,
        fired_n=len(bull),
        median_fwd_return=round(median_bull, 4) if median_bull is not None else None,
        mean_fwd_return=round(mean_bull, 4) if mean_bull is not None else None,
        win_rate=round(win_rate, 3) if win_rate is not None else None,
        median_baseline_return=round(median_baseline, 4),
        median_excess=round(excess, 4) if excess is not None else None,
        hit_ratio=round(hit_ratio, 3) if hit_ratio is not None else None,
        sample_window_days=len(baseline),
        computed_at=datetime.now(timezone.utc).isoformat(),
    )


def run_all(tickers: Optional[list[str]] = None) -> dict:
    """Run every (indicator × ticker × horizon) combination and persist."""
    if tickers is None:
        tickers = list(lt.TICKER_MAP.keys())
    started = time.time()
    out_rows = []
    summary = {"computed": 0, "skipped": 0}
    for ind_name in INDICATORS_TO_BACKTEST:
        for ticker in tickers:
            for h in HORIZONS:
                try:
                    r = backtest_indicator(ind_name, ticker, h)
                except Exception as e:
                    log.warning("backtest %s/%s/%d failed: %s", ind_name, ticker, h, e)
                    summary["skipped"] += 1
                    continue
                if r is None:
                    summary["skipped"] += 1
                    continue
                out_rows.append((
                    r.indicator, r.ticker, r.horizon_days, r.fired_n,
                    r.median_fwd_return, r.mean_fwd_return, r.win_rate,
                    r.median_baseline_return, r.median_excess, r.hit_ratio,
                    r.sample_window_days, r.computed_at,
                ))
                summary["computed"] += 1
    if out_rows:
        db.upsert_backtest_results(out_rows)
    summary["elapsed_s"] = round(time.time() - started, 2)
    return summary


def latest_results() -> list[dict]:
    """Return the most recent backtest result for each (indicator, ticker, horizon)."""
    rows = db.get_latest_backtest_results()
    return [dict(r) for r in rows]
