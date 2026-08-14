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
                       start_date: str, end_date: str, window_days: int = 90,
                       max_concurrent: int | None = None,
                       stop_loss_pct: float | None = None,
                       position_size_pct: float | None = None) -> dict:
    """Run the first-crossing backtest with optional portfolio constraints.

    Additional inputs:
      - max_concurrent: cap on positions held simultaneously. When the cap is
        reached, later signals are dropped (not queued) — realistic for a
        capacity-limited strategy.
      - stop_loss_pct: exit a position early if its ticker return drops below
        -stop_loss_pct at any daily close during the hold window.
      - position_size_pct: capital % allocated per trade. Defaults to
        100 / max_concurrent (equal weight) when max_concurrent is set,
        otherwise 100 (single position at a time is the implicit assumption
        of the unconstrained variant).
    """
    tickers = _candidate_tickers(start_date, end_date)
    if not tickers:
        return {"trades": [], "summary": _empty_summary(
            threshold, hold_days, start_date, end_date,
            max_concurrent=max_concurrent, stop_loss_pct=stop_loss_pct)}

    await prices.ensure_prices_for(tickers)

    # 1) Gather all first-crossing signals sorted by date.
    signals: list[tuple[str, str]] = []  # (signal_date, ticker)
    for t in tickers:
        cross = _first_crossing(t, threshold, start_date, end_date, window_days)
        if cross:
            signals.append((cross, t))
    signals.sort(key=lambda x: x[0])

    # 2) Portfolio simulation.
    if position_size_pct is None:
        position_size_pct = (100.0 / max_concurrent) if max_concurrent else 100.0
    size_frac = float(position_size_pct) / 100.0

    open_positions: dict[str, dict] = {}  # ticker → {buy_d, sell_d, sold, ...}
    closed_trades: list[dict] = []

    end_d = _date(end_date)
    for sig_date, ticker in signals:
        # Close any position whose hold window ended (or stop-loss triggered)
        # before this signal date.
        _close_expired(open_positions, closed_trades, sig_date, hold_days, stop_loss_pct)
        if max_concurrent is not None and len(open_positions) >= max_concurrent:
            continue
        if ticker in open_positions:
            continue
        buy_d = (_date(sig_date) + dt.timedelta(days=1)).isoformat()
        open_positions[ticker] = {
            "signal_date": sig_date,
            "buy_date":    buy_d,
            "size_frac":   size_frac,
            "score":       round(synthesis_score_at(ticker, sig_date, window_days=window_days), 2),
        }

    # Close everything by end_date.
    _close_expired(open_positions, closed_trades, end_date, hold_days, stop_loss_pct,
                   force_by=end_date)

    # 3) Convert to trade rows.
    trades: list[dict] = []
    for c in closed_trades:
        r = _return_pct(c["ticker"], c["buy_date"], c["sell_date"])
        b = _return_pct(BENCHMARK, c["buy_date"], c["sell_date"])
        if r is None or b is None:
            continue
        alpha = r - b
        trades.append({
            "ticker":          c["ticker"],
            "signal_date":     c["signal_date"],
            "buy_date":        c["buy_date"],
            "sell_date":       c["sell_date"],
            "return_pct":      round(r * 100, 3),
            "benchmark_pct":   round(b * 100, 3),
            "alpha_pct":       round(alpha * 100, 3),
            "win":             int(alpha > 0),
            "score_at_signal": c["score"],
            "size_frac":       round(c["size_frac"], 4),
            "sold_reason":     c["sold_reason"],
        })

    summary = _summarise(trades, threshold, hold_days, start_date, end_date,
                        max_concurrent=max_concurrent, stop_loss_pct=stop_loss_pct)
    curve = _equity_curve(trades, start_date, end_date)

    return {"trades": trades, "summary": summary, "equity_curve": curve}


def _close_expired(open_positions: dict, closed_trades: list, as_of: str,
                   hold_days: int, stop_loss_pct: float | None,
                   force_by: str | None = None) -> None:
    """Close positions whose hold window ended by `as_of`, or whose ticker
    return breached the stop-loss at any point in the window. If force_by
    is set, close every remaining position by that date."""
    as_of_d = _date(as_of)
    to_close = []
    for ticker, pos in open_positions.items():
        buy_d = _date(pos["buy_date"])
        natural_sell = buy_d + dt.timedelta(days=hold_days)
        # Check stop-loss up to as_of (or natural_sell, whichever is earlier).
        check_end = min(natural_sell, as_of_d)
        sold_reason = None
        sold_date = None
        if stop_loss_pct is not None and check_end > buy_d:
            # Walk each daily close in [buy+1, check_end] and see if the
            # cumulative return breaches -stop_loss_pct/100.
            threshold_ret = -abs(float(stop_loss_pct)) / 100.0
            p0 = db.get_close_on_or_after(ticker, pos["buy_date"])
            if p0 and p0[1] > 0:
                # Cheap daily walk — okay for MVP-scale backtests.
                d = buy_d + dt.timedelta(days=1)
                while d <= check_end:
                    pn = db.get_close_on_or_before(ticker, d.isoformat())
                    if pn and pn[1] > 0:
                        if (pn[1] / p0[1] - 1.0) <= threshold_ret:
                            sold_reason = "stop_loss"
                            sold_date = d.isoformat()
                            break
                    d += dt.timedelta(days=1)
        if sold_reason:
            pos["sell_date"] = sold_date
            pos["sold_reason"] = sold_reason
        elif natural_sell <= as_of_d:
            pos["sell_date"] = natural_sell.isoformat()
            pos["sold_reason"] = "hold_expiry"
        elif force_by and _date(force_by) <= as_of_d:
            pos["sell_date"] = force_by
            pos["sold_reason"] = "backtest_end"
        else:
            continue
        pos["ticker"] = ticker
        to_close.append((ticker, pos))
    for ticker, pos in to_close:
        closed_trades.append(pos)
        del open_positions[ticker]


def _empty_summary(threshold: float, hold_days: int, s: str, e: str,
                   max_concurrent: int | None = None,
                   stop_loss_pct: float | None = None) -> dict:
    return {
        "threshold":   threshold,
        "hold_days":   hold_days,
        "start_date":  s,
        "end_date":    e,
        "max_concurrent": max_concurrent,
        "stop_loss_pct":  stop_loss_pct,
        "n_trades":    0,
        "win_rate":    0.0,
        "mean_alpha_pct":   0.0,
        "median_alpha_pct": 0.0,
        "best_alpha_pct":   0.0,
        "worst_alpha_pct":  0.0,
        "total_return_pct": 0.0,
        "annualised_alpha_pct": 0.0,
        "sharpe": 0.0,
        "n_stopped_out": 0,
    }


def _summarise(trades: list[dict], threshold: float, hold_days: int,
               start_date: str, end_date: str,
               max_concurrent: int | None = None,
               stop_loss_pct: float | None = None) -> dict:
    if not trades:
        return _empty_summary(threshold, hold_days, start_date, end_date,
                              max_concurrent=max_concurrent,
                              stop_loss_pct=stop_loss_pct)
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
    n_stopped = sum(1 for t in trades if t.get("sold_reason") == "stop_loss")
    return {
        "threshold":   threshold,
        "hold_days":   hold_days,
        "start_date":  start_date,
        "end_date":    end_date,
        "max_concurrent": max_concurrent,
        "stop_loss_pct":  stop_loss_pct,
        "n_trades":    len(trades),
        "n_stopped_out": n_stopped,
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
