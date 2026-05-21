"""Backtest the synthesis score.

Question we want to answer: if we'd hypothetically bought every ticker
whose synthesis score crossed >= threshold in some date range, and held
for `hold_days`, what would the realised alpha have been versus SPY?

Strategy implemented (first-crossing variant):
  - For each ticker that has any signal data, find the earliest date in
    [start_date, end_date] where synthesis_score_at(t, date) >= threshold.
  - Treat that as a buy at the next trading day's close.
  - Compute forward return over hold_days and the matching SPY return.
  - Alpha = ticker_return - spy_return.

We aggregate across all trades:
  - win_rate, mean alpha, median alpha, total return (compounded as if
    we'd allocated equal weight to each trade)
  - daily equity curve where each active trade contributes its running
    daily alpha; useful for plotting.

The first-crossing rule keeps it tractable (one trade per ticker) and
honest (no look-ahead, no re-buys after threshold momentarily dips below
and crosses again).
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from typing import Iterable

import db
import prices
from signals import synthesis_score_at

log = logging.getLogger("backtest")

BENCHMARK = "SPY"


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s[:10])


def _isodate(d: dt.date) -> str:
    return d.isoformat()


def _candidate_tickers(start_date: str, end_date: str) -> list[str]:
    """Tickers that have any signal activity in the window."""
    with db.connect() as cx:
        rows = cx.execute(
            """
            SELECT issuer_ticker AS ticker FROM insider_txn
              WHERE issuer_ticker IS NOT NULL
                AND filed_at >= ? AND filed_at < datetime(?, '+1 day')
            UNION
            SELECT issuer_ticker AS ticker FROM activist_stake
              WHERE issuer_ticker IS NOT NULL
                AND filed_at >= ? AND filed_at < datetime(?, '+1 day')
            UNION
            SELECT issuer_ticker AS ticker FROM ma_event
              WHERE issuer_ticker IS NOT NULL
                AND filed_at >= ? AND filed_at < datetime(?, '+1 day')
            UNION
            SELECT ticker FROM congress_trade
              WHERE ticker IS NOT NULL
                AND disclosure_date >= ? AND disclosure_date <= ?
            UNION
            SELECT ticker FROM options_flow_trade
              WHERE ticker IS NOT NULL
                AND alerted_at >= ? AND alerted_at < datetime(?, '+1 day')
            UNION
            SELECT ticker FROM dark_pool_print
              WHERE ticker IS NOT NULL
                AND executed_at >= ? AND executed_at < datetime(?, '+1 day')
            """,
            (start_date, end_date) * 6,
        ).fetchall()
    return sorted({r["ticker"] for r in rows if r["ticker"]})


def _signal_dates_for_ticker(ticker: str, start_date: str, end_date: str) -> list[str]:
    """All dates in the window when *this* ticker had any signal landing.

    The synthesis score only changes on signal dates, so we only need to
    recompute on those. Big perf win over daily-loop.
    """
    with db.connect() as cx:
        rows = cx.execute(
            """
            SELECT substr(filed_at, 1, 10) AS d FROM insider_txn
              WHERE issuer_ticker = ?
                AND filed_at >= ? AND filed_at < datetime(?, '+1 day')
            UNION
            SELECT substr(filed_at, 1, 10) AS d FROM activist_stake
              WHERE issuer_ticker = ?
                AND filed_at >= ? AND filed_at < datetime(?, '+1 day')
            UNION
            SELECT substr(filed_at, 1, 10) AS d FROM ma_event
              WHERE issuer_ticker = ?
                AND filed_at >= ? AND filed_at < datetime(?, '+1 day')
            UNION
            SELECT disclosure_date AS d FROM congress_trade
              WHERE ticker = ?
                AND disclosure_date >= ? AND disclosure_date <= ?
            UNION
            SELECT substr(alerted_at, 1, 10) AS d FROM options_flow_trade
              WHERE ticker = ?
                AND alerted_at >= ? AND alerted_at < datetime(?, '+1 day')
            UNION
            SELECT substr(executed_at, 1, 10) AS d FROM dark_pool_print
              WHERE ticker = ?
                AND executed_at >= ? AND executed_at < datetime(?, '+1 day')
            """,
            (ticker, start_date, end_date) * 6,
        ).fetchall()
    return sorted({r["d"] for r in rows if r["d"]})


def _first_crossing(ticker: str, threshold: float, start_date: str, end_date: str,
                    window_days: int) -> str | None:
    """Earliest date in [start, end] where synthesis(ticker, date) >= threshold."""
    for d in _signal_dates_for_ticker(ticker, start_date, end_date):
        if d < start_date or d > end_date:
            continue
        score = synthesis_score_at(ticker, d, window_days=window_days)
        if score >= threshold:
            return d
    return None


def _return_pct(ticker: str, from_date: str, to_date: str) -> float | None:
    p0 = db.get_close_on_or_after(ticker, from_date)
    p1 = db.get_close_on_or_after(ticker, to_date)
    if not (p0 and p1) or p0[1] <= 0:
        return None
    return (p1[1] / p0[1]) - 1.0


async def run_backtest(*, threshold: float, hold_days: int,
                       start_date: str, end_date: str, window_days: int = 90) -> dict:
    """Run the first-crossing backtest. Returns trades + summary + equity curve."""
    tickers = _candidate_tickers(start_date, end_date)
    if not tickers:
        return {"trades": [], "summary": _empty_summary(threshold, hold_days, start_date, end_date)}

    # Make sure prices are local for every ticker we might evaluate plus SPY.
    await prices.ensure_prices_for(tickers)

    trades: list[dict] = []
    for t in tickers:
        cross = _first_crossing(t, threshold, start_date, end_date, window_days)
        if not cross:
            continue
        # Buy on cross_date + 1 trading day (use price on-or-after cross+1).
        buy_d = (_date(cross) + dt.timedelta(days=1)).isoformat()
        sell_d = (_date(cross) + dt.timedelta(days=1 + hold_days)).isoformat()
        r = _return_pct(t, buy_d, sell_d)
        b = _return_pct(BENCHMARK, buy_d, sell_d)
        if r is None or b is None:
            continue
        alpha = r - b
        trades.append({
            "ticker":          t,
            "signal_date":     cross,
            "buy_date":        buy_d,
            "sell_date":       sell_d,
            "return_pct":      round(r * 100, 3),
            "benchmark_pct":   round(b * 100, 3),
            "alpha_pct":       round(alpha * 100, 3),
            "win":             int(alpha > 0),
            "score_at_signal": round(synthesis_score_at(t, cross, window_days=window_days), 2),
        })

    summary = _summarise(trades, threshold, hold_days, start_date, end_date)
    curve = _equity_curve(trades, start_date, end_date)

    return {"trades": trades, "summary": summary, "equity_curve": curve}


def _empty_summary(threshold: float, hold_days: int, s: str, e: str) -> dict:
    return {
        "threshold":   threshold,
        "hold_days":   hold_days,
        "start_date":  s,
        "end_date":    e,
        "n_trades":    0,
        "win_rate":    0.0,
        "mean_alpha_pct":   0.0,
        "median_alpha_pct": 0.0,
        "best_alpha_pct":   0.0,
        "worst_alpha_pct":  0.0,
        "total_return_pct": 0.0,
        "annualised_alpha_pct": 0.0,
        "sharpe": 0.0,
    }


def _summarise(trades: list[dict], threshold: float, hold_days: int,
               start_date: str, end_date: str) -> dict:
    if not trades:
        return _empty_summary(threshold, hold_days, start_date, end_date)
    alphas = [t["alpha_pct"] for t in trades]
    wins = sum(t["win"] for t in trades)
    # Equal-weighted compounded total alpha — sums of per-trade alpha
    # /100 because alphas are already in pct. With overlapping holds the
    # naive sum overstates; for a first-pass metric it's close enough.
    total = sum(a / 100.0 for a in alphas)
    # Annualise: assume each trade ties up `hold_days` days; we have
    # n_trades trades total. Approx active capital = n × hold_days; this
    # is rough but useful.
    days_active = max(1, (_date(end_date) - _date(start_date)).days)
    annualised = (total * (365.0 / days_active)) * 100.0
    # Sharpe: alpha mean / alpha std × sqrt(observations per year). Treat
    # each trade as one observation; rough but interpretable.
    if len(alphas) >= 2:
        mu = statistics.mean(alphas)
        sd = statistics.pstdev(alphas) or 1e-9
        sharpe = (mu / sd) * ((365.0 / hold_days) ** 0.5)
    else:
        sharpe = 0.0
    return {
        "threshold":   threshold,
        "hold_days":   hold_days,
        "start_date":  start_date,
        "end_date":    end_date,
        "n_trades":    len(trades),
        "win_rate":    round(wins / len(trades), 4),
        "mean_alpha_pct":   round(statistics.mean(alphas), 3),
        "median_alpha_pct": round(statistics.median(alphas), 3),
        "best_alpha_pct":   round(max(alphas), 3),
        "worst_alpha_pct":  round(min(alphas), 3),
        "total_return_pct": round(total * 100, 3),
        "annualised_alpha_pct": round(annualised, 3),
        "sharpe":      round(sharpe, 3),
    }


def _equity_curve(trades: list[dict], start_date: str, end_date: str) -> list[dict]:
    """Daily equity curve, equal-weighted across active trades.

    Walks day-by-day from start_date to end_date. On each day, sums the
    per-trade alpha (relative to SPY) accrued over that single day for
    every trade that is currently held (buy_date <= day < sell_date).
    The portfolio compounds these daily contributions.
    """
    if not trades:
        return []

    start = _date(start_date)
    end = _date(end_date)
    if end < start:
        return []

    # Pre-build a per-trade daily series of (date, ticker_close, spy_close).
    trade_series: list[dict] = []
    for t in trades:
        buy = _date(t["buy_date"])
        sell = _date(t["sell_date"])
        trade_series.append({"ticker": t["ticker"], "buy": buy, "sell": sell})

    out: list[dict] = []
    equity = 1.0
    prev_day = None
    cur = start
    while cur <= end:
        day = cur.isoformat()
        active = [ts for ts in trade_series if ts["buy"] <= cur < ts["sell"]]
        daily_alpha = 0.0
        if active and prev_day is not None:
            contribs = []
            for ts in active:
                p_prev = db.get_close_on_or_before(ts["ticker"], prev_day)
                p_cur  = db.get_close_on_or_before(ts["ticker"], day)
                spy_prev = db.get_close_on_or_before(BENCHMARK, prev_day)
                spy_cur  = db.get_close_on_or_before(BENCHMARK, day)
                if not (p_prev and p_cur and spy_prev and spy_cur):
                    continue
                if p_prev[1] <= 0 or spy_prev[1] <= 0:
                    continue
                tr = (p_cur[1] / p_prev[1]) - 1.0
                br = (spy_cur[1] / spy_prev[1]) - 1.0
                contribs.append(tr - br)
            if contribs:
                daily_alpha = sum(contribs) / len(contribs)
        equity *= (1.0 + daily_alpha)
        out.append({"date": day, "equity": round(equity, 6),
                    "active_trades": len(active)})
        prev_day = day
        cur += dt.timedelta(days=1)

    return out
