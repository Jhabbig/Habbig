#!/usr/bin/env python3
"""
Portfolio performance + benchmark comparison.

Answers the question every HODLer asks: "Am I winning?"

Model:
  - Each lot in `crypto_holdings` is treated as a cash deposit event at
    `acquired_at` of size `qty × cost_basis`. The user "added" that
    capital to the portfolio at that moment.
  - Each disposition is an internal swap (position → cash). It doesn't
    affect total invested capital; it does affect the position-vs-cash
    split inside the portfolio.
  - Daily portfolio value = cash from dispositions + Σ(remaining lot
    qty × close price) for each open lot, evaluated on each day.

Outputs:
  - **Total return**: (current value + realised cash − total deposited) /
    total deposited.
  - **TWRR** (time-weighted return rate): geometric mean of period
    returns, where periods are bounded by cash-flow events. This is the
    industry-standard return metric — it strips out the user's deposit
    timing and measures pure asset performance.
  - **Equity curve**: daily value time series since inception.
  - **Sharpe / Sortino**: computed on daily portfolio returns.
  - **Max drawdown**: peak-to-trough on the equity curve.
  - **Benchmarks**: same-dollars HODL in BTC, weekly DCA equivalent,
    same-dollars in SPY. Each rendered as its own equity curve so the
    user can see whether their stock picking added or subtracted alpha
    vs the simplest passive strategies.

Notes:
  - This module is pure-Python + numpy; it reads from the holdings,
    dispositions, and daily-bar tables but doesn't write anything.
  - "Inception" = date of the user's earliest lot. Before that, the
    portfolio is undefined.
  - We don't model fiat fluctuations — everything is denominated in
    USD assuming dollar-pegged cash.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np

import database as db
import long_term as lt

log = logging.getLogger("crypto.performance")

# Risk-free rate for Sharpe/Sortino. Matches long_term.RF_RATE.
RF_RATE = 0.04


# ─── Cash-flow + equity curve ───────────────────────────────────────────────

@dataclass
class CashFlow:
    date: str             # ISO date
    amount: float         # positive = deposit, negative = withdrawal
    kind: str             # 'deposit' | 'withdraw' | 'swap'
    ticker: Optional[str] # asset bought/sold (for context)


def _cash_flows(user_id: str) -> list[CashFlow]:
    """Build the cash-flow series. Each lot acquisition is a deposit; each
    disposition is a swap (no external cash flow). Sorted oldest→newest."""
    flows: list[CashFlow] = []
    for h in db.get_holdings(user_id):
        amt = float(h["qty"]) * float(h["cost_basis"])
        flows.append(CashFlow(
            date=h["acquired_at"], amount=amt, kind="deposit",
            ticker=h["ticker"],
        ))
    # Dispositions don't add capital — they convert position into cash
    # *inside* the portfolio. We don't emit a CashFlow for them.
    flows.sort(key=lambda f: f.date)
    return flows


def _daily_value_series(user_id: str) -> tuple[list[str], list[float], list[float]]:
    """Build the daily portfolio-value series since the earliest deposit.
    Returns (dates, position_value_usd, cash_balance_usd)."""
    holdings = db.get_holdings(user_id)
    if not holdings:
        return [], [], []
    dispositions = db.get_dispositions(user_id, limit=10_000)

    inception = min(h["acquired_at"] for h in holdings)
    today = datetime.now(timezone.utc).date().isoformat()
    # Pre-load close prices for every ticker present in holdings.
    tickers = sorted({h["ticker"] for h in holdings})
    closes_by_ticker: dict[str, dict[str, float]] = {}
    for t in tickers:
        rows = db.get_daily_bars(t, days=365 * 5)
        closes_by_ticker[t] = {r["date"]: float(r["close"]) for r in rows}

    # Holdings consumption per disposition, keyed by holding_id.
    consumption_map = db.get_consumption_by_holding(user_id)

    # Build the date axis.
    start_d = datetime.fromisoformat(inception).date()
    end_d = datetime.now(timezone.utc).date()
    days = (end_d - start_d).days + 1
    if days <= 0:
        return [], [], []
    dates = [(start_d + timedelta(days=i)).isoformat() for i in range(days)]

    # For each holding, on each date, compute remaining qty. For correctness
    # the consumption should be allocated to specific dates (the disposition
    # date), but `crypto_tax_lot_consumption` doesn't carry the disposition
    # date directly; we need to join. For simplicity we approximate: a lot's
    # full qty is held from acquisition until the LAST disposition that
    # consumed any of it, then drops to (qty - consumed). For multi-step
    # consumption this slightly overstates qty mid-window — acceptable for
    # the v1 equity curve.
    position: list[float] = [0.0] * days
    cash: list[float] = [0.0] * days

    # Cumulative cash from dispositions over time.
    sorted_disp = sorted(dispositions, key=lambda d: d["sell_date"])
    disp_idx = 0
    running_cash = 0.0

    last_known_close: dict[str, float] = {}
    for di, d in enumerate(dates):
        # Apply any dispositions that landed on or before this date.
        while disp_idx < len(sorted_disp) and sorted_disp[disp_idx]["sell_date"] <= d:
            sd = sorted_disp[disp_idx]
            running_cash += float(sd["qty"]) * float(sd["sell_price"])
            disp_idx += 1
        cash[di] = running_cash

        # Sum position value across all open lots.
        total_value = 0.0
        for h in holdings:
            if h["acquired_at"] > d:
                continue  # lot hadn't been bought yet
            # Approximate remaining qty: full lot.qty until consumed. After
            # consumption events we drop to (qty - total_consumed). This is
            # a simplification — see comment above.
            remaining = float(h["qty"]) - float(consumption_map.get(h["id"], 0.0))
            if remaining <= 0:
                continue
            # Look up close price; forward-fill if missing (weekends + gaps).
            ticker = h["ticker"]
            close_map = closes_by_ticker.get(ticker, {})
            price = close_map.get(d)
            if price is None:
                price = last_known_close.get(ticker)
            else:
                last_known_close[ticker] = price
            if price is None:
                continue
            total_value += remaining * price
        position[di] = total_value
    return dates, position, cash


# ─── Returns + risk metrics ─────────────────────────────────────────────────

def _twrr(equity: np.ndarray, deposits: np.ndarray) -> Optional[float]:
    """Time-weighted return rate. Periods are bounded by deposit dates;
    within each period return = V_end / V_start. Product − 1 = TWRR.
    Pure asset performance, strips out deposit timing."""
    if len(equity) < 2:
        return None
    # Identify deposit dates (any day where the cumulative-deposit series
    # jumped).
    deposit_dates = np.where(np.diff(deposits, prepend=deposits[0]) > 0)[0]
    if len(deposit_dates) == 0:
        # Single-day or constant deposit — just return total period return.
        if equity[0] <= 0:
            return None
        return float(equity[-1] / equity[0] - 1.0)

    periods = []
    prev = 0
    for d in deposit_dates:
        if d > prev:
            v0 = equity[prev]
            # On a deposit day, the equity jumps by the deposit amount.
            # The TWRR period ends *before* the deposit lands, so we use
            # the value just before the cash flow added.
            v_end = equity[d] - (deposits[d] - deposits[d - 1] if d > 0 else 0)
            if v0 > 0:
                periods.append(v_end / v0)
        prev = d
    # Final period: from last deposit to today.
    v0 = equity[prev]
    v_end = equity[-1]
    if v0 > 0:
        periods.append(v_end / v0)
    if not periods:
        return None
    twrr = 1.0
    for r in periods:
        twrr *= max(0.0, r)
    return float(twrr - 1.0)


def _sharpe(daily_returns: np.ndarray) -> Optional[float]:
    if len(daily_returns) < 30:
        return None
    excess = daily_returns - (RF_RATE / 365.0)
    sd = float(np.std(excess, ddof=1))
    if sd == 0:
        return None
    return float(np.mean(excess) / sd * math.sqrt(365))


def _sortino(daily_returns: np.ndarray) -> Optional[float]:
    if len(daily_returns) < 30:
        return None
    excess = daily_returns - (RF_RATE / 365.0)
    downside = excess[excess < 0]
    if len(downside) < 5:
        return None
    dd = float(np.sqrt(np.mean(downside ** 2)))
    if dd == 0:
        return None
    return float(np.mean(excess) / dd * math.sqrt(365))


def _max_dd(equity: np.ndarray) -> float:
    if len(equity) < 2:
        return 0.0
    running = np.maximum.accumulate(equity)
    safe_running = np.where(running > 0, running, 1)
    dd = (equity - running) / safe_running
    return float(np.min(dd))


def _period_return(dates: list[str], equity: np.ndarray,
                    deposits: np.ndarray, days_back: int) -> Optional[float]:
    """Return over the last `days_back` days, using deposits-adjusted TWRR.
    For short windows (≤ 365d) where the user may have made deposits, we
    chain the daily returns over the window."""
    if len(equity) <= 1:
        return None
    start_i = max(0, len(equity) - days_back - 1)
    if equity[start_i] <= 0:
        return None
    # Adjust for cash flows in the window.
    sub_equity = equity[start_i:]
    sub_deposits = deposits[start_i:] - deposits[start_i]
    return _twrr(sub_equity, sub_deposits)


# ─── Benchmarks ─────────────────────────────────────────────────────────────

def _benchmark_curve(user_id: str, dates: list[str], deposits: np.ndarray,
                     benchmark_ticker: str) -> Optional[np.ndarray]:
    """Replay the user's deposit schedule into `benchmark_ticker` and return
    the daily-value curve. If on day t the user deposited $X, we buy $X
    worth of benchmark at that day's close and add to the holding."""
    if not dates:
        return None
    # Build a close-price map for the benchmark.
    if benchmark_ticker in lt.TICKER_MAP:
        rows = db.get_daily_bars(benchmark_ticker, days=365 * 5)
    else:
        rows = db.get_cross_asset_bars(benchmark_ticker, days=365 * 5)
    if not rows:
        return None
    close_map = {r["date"]: float(r["close"]) for r in rows}
    if not close_map:
        return None

    qty = 0.0
    last_price = None
    curve = np.zeros(len(dates))
    prev_dep = 0.0
    for i, d in enumerate(dates):
        price = close_map.get(d)
        if price is None:
            price = last_price
        if price is None:
            # No price yet — carry forward as zero. Will fill once we
            # find a close.
            continue
        last_price = price
        flow = deposits[i] - prev_dep
        prev_dep = deposits[i]
        if flow > 0:
            qty += flow / price
        curve[i] = qty * price
    return curve


def _dca_curve(dates: list[str], total_deposits: float,
               benchmark_ticker: str = "BTC") -> Optional[np.ndarray]:
    """Equal-amount weekly DCA equivalent over the full window. Spreads
    `total_deposits` evenly across weekly buys, then holds."""
    if not dates or total_deposits <= 0:
        return None
    if benchmark_ticker in lt.TICKER_MAP:
        rows = db.get_daily_bars(benchmark_ticker, days=365 * 5)
    else:
        rows = db.get_cross_asset_bars(benchmark_ticker, days=365 * 5)
    if not rows:
        return None
    close_map = {r["date"]: float(r["close"]) for r in rows}
    n_weeks = max(1, len(dates) // 7)
    weekly_dep = total_deposits / n_weeks
    qty = 0.0
    last_price = None
    curve = np.zeros(len(dates))
    for i, d in enumerate(dates):
        price = close_map.get(d) or last_price
        if price is None:
            continue
        last_price = price
        # Every 7th day, buy weekly_dep / price.
        if i % 7 == 0:
            qty += weekly_dep / price
        curve[i] = qty * price
    return curve


# ─── Public API ─────────────────────────────────────────────────────────────

@dataclass
class PerformanceOverview:
    inception_date: Optional[str]
    days_invested: int
    total_deposited_usd: float
    realised_cash_usd: float
    position_value_usd: float
    total_value_usd: float
    total_return_pct: Optional[float]
    twrr_pct: Optional[float]
    return_7d_pct: Optional[float]
    return_30d_pct: Optional[float]
    return_ytd_pct: Optional[float]
    return_1y_pct: Optional[float]
    sharpe: Optional[float]
    sortino: Optional[float]
    max_drawdown_pct: float
    # Benchmark comparisons (TWRR over same window)
    hodl_btc_pct: Optional[float]
    dca_btc_pct: Optional[float]
    spy_pct: Optional[float]
    excess_vs_hodl_btc_pct: Optional[float]
    excess_vs_spy_pct: Optional[float]

    def to_dict(self) -> dict:
        return asdict(self)


def overview(user_id: str) -> dict:
    dates, position, cash = _daily_value_series(user_id)
    if not dates:
        return {"ready": False, "reason": "no holdings yet"}
    equity = np.asarray(position, dtype=np.float64) + np.asarray(cash, dtype=np.float64)
    flows = _cash_flows(user_id)
    deposits = _cumulative_deposits(dates, flows)
    total_deposited = float(deposits[-1]) if len(deposits) else 0.0

    # Daily portfolio returns adjusted for cash flows: r_t = (V_t − F_t) / V_{t−1} − 1
    daily_returns = _daily_returns_cf_adjusted(equity, deposits)

    twrr = _twrr(equity, deposits)
    total_return = ((equity[-1] / total_deposited - 1.0)
                    if total_deposited > 0 else None)

    # Benchmarks: replay the user's deposit schedule into BTC and SPY.
    hodl_curve = _benchmark_curve(user_id, dates, deposits, "BTC")
    spy_curve = _benchmark_curve(user_id, dates, deposits, "SPY")
    dca_curve = _dca_curve(dates, total_deposited, "BTC")

    def _curve_return(c):
        if c is None or len(c) == 0 or total_deposited <= 0:
            return None
        return float(c[-1] / total_deposited - 1.0)

    hodl_ret = _curve_return(hodl_curve)
    spy_ret = _curve_return(spy_curve)
    dca_ret = _curve_return(dca_curve)
    excess_hodl = (twrr - hodl_ret) if (twrr is not None and hodl_ret is not None) else None
    excess_spy = (twrr - spy_ret) if (twrr is not None and spy_ret is not None) else None

    ov = PerformanceOverview(
        inception_date=dates[0],
        days_invested=len(dates),
        total_deposited_usd=round(total_deposited, 2),
        realised_cash_usd=round(float(cash[-1]), 2),
        position_value_usd=round(float(position[-1]), 2),
        total_value_usd=round(float(equity[-1]), 2),
        total_return_pct=round(total_return, 4) if total_return is not None else None,
        twrr_pct=round(twrr, 4) if twrr is not None else None,
        return_7d_pct=_round(_period_return(dates, equity, deposits, 7)),
        return_30d_pct=_round(_period_return(dates, equity, deposits, 30)),
        return_ytd_pct=_round(_ytd_return(dates, equity, deposits)),
        return_1y_pct=_round(_period_return(dates, equity, deposits, 365)),
        sharpe=_sharpe(daily_returns),
        sortino=_sortino(daily_returns),
        max_drawdown_pct=round(_max_dd(equity), 4),
        hodl_btc_pct=_round(hodl_ret),
        dca_btc_pct=_round(dca_ret),
        spy_pct=_round(spy_ret),
        excess_vs_hodl_btc_pct=_round(excess_hodl),
        excess_vs_spy_pct=_round(excess_spy),
    )
    return {"ready": True, **ov.to_dict()}


def equity_curve(user_id: str, include: list[str] | None = None) -> dict:
    """Daily equity curve for the user's portfolio + the named benchmarks.
    `include` is a list of benchmark names: 'HODL_BTC', 'DCA_BTC', 'SPY'.
    Defaults to all three. Returns aligned arrays for charting."""
    dates, position, cash = _daily_value_series(user_id)
    if not dates:
        return {"ready": False}
    equity = np.asarray(position, dtype=np.float64) + np.asarray(cash, dtype=np.float64)
    flows = _cash_flows(user_id)
    deposits = _cumulative_deposits(dates, flows)
    total = float(deposits[-1]) if len(deposits) else 0.0

    include = include or ["HODL_BTC", "DCA_BTC", "SPY"]
    series: dict[str, list[float]] = {
        "portfolio": [round(float(v), 2) for v in equity],
    }
    if "HODL_BTC" in include:
        c = _benchmark_curve(user_id, dates, deposits, "BTC")
        if c is not None:
            series["HODL_BTC"] = [round(float(v), 2) for v in c]
    if "DCA_BTC" in include and total > 0:
        c = _dca_curve(dates, total, "BTC")
        if c is not None:
            series["DCA_BTC"] = [round(float(v), 2) for v in c]
    if "SPY" in include:
        c = _benchmark_curve(user_id, dates, deposits, "SPY")
        if c is not None:
            series["SPY"] = [round(float(v), 2) for v in c]
    return {
        "ready": True, "dates": dates, "series": series,
        "deposits_cumulative": [round(float(v), 2) for v in deposits],
    }


# ─── Helpers ────────────────────────────────────────────────────────────────

def _cumulative_deposits(dates: list[str], flows: list[CashFlow]) -> np.ndarray:
    """Cumulative-deposit series aligned to the date axis."""
    out = np.zeros(len(dates), dtype=np.float64)
    if not flows:
        return out
    j = 0
    running = 0.0
    sorted_flows = sorted(flows, key=lambda f: f.date)
    for i, d in enumerate(dates):
        while j < len(sorted_flows) and sorted_flows[j].date <= d:
            running += sorted_flows[j].amount
            j += 1
        out[i] = running
    return out


def _daily_returns_cf_adjusted(equity: np.ndarray, deposits: np.ndarray) -> np.ndarray:
    """Daily returns adjusted for cash flows. On a deposit day, the equity
    jumps by the deposit amount — that's not a real return. So we subtract
    the deposit delta before computing the return."""
    if len(equity) < 2:
        return np.array([])
    flow_delta = np.diff(deposits, prepend=deposits[0])
    # Adjusted equity for return computation: V_t − F_t where F_t is today's
    # net deposit (so we measure pure asset performance).
    adj = equity - flow_delta
    out = []
    for i in range(1, len(equity)):
        v_prev = equity[i - 1]
        if v_prev <= 0:
            continue
        out.append(adj[i] / v_prev - 1.0)
    return np.asarray(out, dtype=np.float64)


def _ytd_return(dates: list[str], equity: np.ndarray, deposits: np.ndarray) -> Optional[float]:
    if not dates:
        return None
    year = datetime.now(timezone.utc).year
    target = f"{year}-01-01"
    idx = None
    for i, d in enumerate(dates):
        if d >= target:
            idx = i
            break
    if idx is None or idx >= len(equity) - 1:
        return None
    sub_equity = equity[idx:]
    sub_deposits = deposits[idx:] - deposits[idx]
    return _twrr(sub_equity, sub_deposits)


def _round(v: Optional[float], digits: int = 4) -> Optional[float]:
    if v is None or not isinstance(v, (int, float)) or math.isnan(v) or math.isinf(v):
        return None
    return round(float(v), digits)
