"""Backtesting engine (F13).

Simulates trading based on historical predictions, market snapshots, and
resolution outcomes. Given a set of parameters (min credibility, min edge,
category, bet sizing), answers: "If you had followed narve.ai's top signals,
what would your returns have been?"

Runs as an async job since computation may take a few seconds on large datasets.
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Optional

import db

log = logging.getLogger("intelligence.backtester")


def run_backtest(params: dict) -> dict:
    """Simulate trading on historical data.

    params:
        min_credibility: float (0-1, default 0.5)
        min_edge: float (absolute, default 0.05)
        category: Optional[str] (None = all categories)
        bet_sizing: "flat" | "kelly" | "half_kelly" (default "flat")
        bankroll: float (starting capital, default 10000)
        max_bet_pct: float (max % of bankroll per trade, default 0.1)

    Returns:
        total_return, sharpe_ratio, win_rate, max_drawdown, trade_count, trade_log
    """
    min_cred = params.get("min_credibility", 0.5)
    min_edge = params.get("min_edge", 0.05)
    category = params.get("category")
    sizing_method = params.get("bet_sizing", "flat")
    bankroll = float(params.get("bankroll", 10000))
    max_bet_pct = float(params.get("max_bet_pct", 0.1))

    initial_bankroll = bankroll

    # Get all resolved predictions with source credibility
    with db.conn() as c:
        where = ["p.resolved = 1", "p.resolved_correct IS NOT NULL", "p.market_id IS NOT NULL"]
        sql_params: list = []
        if category:
            where.append("p.category = ?")
            sql_params.append(category)

        rows = c.execute(
            "SELECT p.*, sc.global_credibility, sc.decay_weighted_accuracy "
            "FROM predictions p "
            "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY p.extracted_at ASC",
            tuple(sql_params),
        ).fetchall()

    if not rows:
        return _empty_result("No resolved predictions found matching criteria")

    # Group predictions by market_id to avoid double-counting
    market_predictions: dict = {}  # market_id -> list of predictions
    for r in rows:
        mid = r["market_id"]
        if mid not in market_predictions:
            market_predictions[mid] = []
        market_predictions[mid].append(r)

    # Simulate trades
    trades = []
    peak_bankroll = bankroll

    for market_id, preds in market_predictions.items():
        # Filter by credibility threshold
        qualified = [p for p in preds if (p["global_credibility"] or 0) >= min_cred]
        if not qualified:
            continue

        # Compute betyc probability for this market
        pred_dicts = [
            {
                "source_handle": p["source_handle"],
                "direction": p["direction"],
                "predicted_probability": p["predicted_probability"],
                "global_credibility": p["global_credibility"],
                "accuracy_unlocked": bool(p.get("decay_weighted_accuracy")),
            }
            for p in qualified
        ]
        result = db.calculate_betyc_probability(pred_dicts)
        betyc_yes = result.get("betyc_yes_probability")
        if betyc_yes is None:
            continue

        # Get market price at the time of the earliest prediction
        slug = market_id.split(":", 1)[1] if ":" in market_id else market_id
        snap = db.get_market_snapshot_at(slug, qualified[0]["extracted_at"])
        if not snap:
            # Try latest
            snap = db.get_latest_market_snapshot(slug)
        if not snap:
            continue

        market_price = snap["yes_price"]
        if not market_price or market_price <= 0 or market_price >= 1:
            continue

        edge = betyc_yes - market_price
        if abs(edge) < min_edge:
            continue

        # Determine trade direction and compute bet size
        bet_yes = edge > 0
        true_prob = betyc_yes if bet_yes else (1 - betyc_yes)
        odds_price = market_price if bet_yes else (1 - market_price)

        # Bet sizing
        if sizing_method == "kelly":
            b = (1 / odds_price) - 1
            kelly_f = (true_prob * b - (1 - true_prob)) / b if b > 0 else 0
            bet_pct = max(0, min(kelly_f, max_bet_pct))
        elif sizing_method == "half_kelly":
            b = (1 / odds_price) - 1
            kelly_f = (true_prob * b - (1 - true_prob)) / b if b > 0 else 0
            bet_pct = max(0, min(kelly_f * 0.5, max_bet_pct))
        else:  # flat
            bet_pct = max_bet_pct

        bet_amount = bankroll * bet_pct
        if bet_amount < 1:
            continue

        # Determine outcome from the first prediction's resolved_correct
        # (All predictions for this market should agree on the outcome)
        outcome_yes = bool(qualified[0]["resolved_correct"]) if qualified[0]["direction"] == "YES" else not bool(qualified[0]["resolved_correct"])

        won = (bet_yes and outcome_yes) or (not bet_yes and not outcome_yes)
        payout = (bet_amount / odds_price) - bet_amount if won else -bet_amount

        bankroll += payout
        peak_bankroll = max(peak_bankroll, bankroll)

        trades.append({
            "market_id": market_id,
            "direction": "YES" if bet_yes else "NO",
            "edge": round(edge, 4),
            "bet_amount": round(bet_amount, 2),
            "odds_price": round(odds_price, 4),
            "won": won,
            "payout": round(payout, 2),
            "bankroll_after": round(bankroll, 2),
        })

    if not trades:
        return _empty_result("No trades qualified after filtering")

    # Compute metrics
    total_return = (bankroll - initial_bankroll) / initial_bankroll
    wins = sum(1 for t in trades if t["won"])
    win_rate = wins / len(trades)

    # Max drawdown
    running_peak = initial_bankroll
    max_dd = 0
    running_bankroll = initial_bankroll
    for t in trades:
        running_bankroll = t["bankroll_after"]
        running_peak = max(running_peak, running_bankroll)
        dd = (running_peak - running_bankroll) / running_peak if running_peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Sharpe-like ratio (mean return / std dev of returns)
    returns = [t["payout"] / max(t["bet_amount"], 1) for t in trades]
    if len(returns) > 1:
        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1))
        sharpe = mean_r / std_r if std_r > 0 else 0
    else:
        sharpe = 0

    return {
        "total_return": round(total_return, 4),
        "total_return_pct": f"{total_return * 100:.1f}%",
        "final_bankroll": round(bankroll, 2),
        "initial_bankroll": initial_bankroll,
        "sharpe_ratio": round(sharpe, 4),
        "win_rate": round(win_rate, 4),
        "max_drawdown": round(max_dd, 4),
        "trade_count": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "best_trade": max(trades, key=lambda t: t["payout"]) if trades else None,
        "worst_trade": min(trades, key=lambda t: t["payout"]) if trades else None,
        "trades": trades[:100],  # cap log for storage
    }


def _empty_result(reason: str) -> dict:
    return {
        "total_return": 0,
        "total_return_pct": "0.0%",
        "final_bankroll": 0,
        "initial_bankroll": 0,
        "sharpe_ratio": 0,
        "win_rate": 0,
        "max_drawdown": 0,
        "trade_count": 0,
        "wins": 0,
        "losses": 0,
        "trades": [],
        "note": reason,
    }
