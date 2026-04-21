"""Backtest engine — historical-replay strategy simulation.

Pure-function core + a DB-backed ``run_backtest`` wrapper the async job
calls. Every numeric output is a plain float so the frontend's Chart.js
overlay can render equity curves without additional server rendering.

Spec inputs:
  params = {
    min_credibility:       float
    min_ev:                float         — skip predictions whose edge is below this
    min_hours_remaining:   float         — skip predictions too close to close
    categories:            list[str]     — empty = all
    source_handles:        list[str]     — empty = all
    bet_sizing:            "flat" | "kelly" | "proportional_ev"
    flat_bet_size:         float
    starting_bankroll:     float
    date_from:             unix seconds
    date_to:               unix seconds
  }

Output:
  {
    params, final_bankroll, roi_pct, win_rate, bet_count,
    sharpe, max_drawdown,
    equity_curve: [{ts, bankroll}, ...],
    bets:         [ ... ]
  }

Kelly fraction: f = (p*b − q) / b where b = (1/market_price) − 1, capped
at 0.25. If p ≤ market_price the Kelly bet is 0.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("backtest")


# ── Kelly + sizing helpers ──────────────────────────────────────────────────


def kelly_fraction(p: float, market_price: float) -> float:
    """Kelly f* for a YES bet at ``market_price`` when we believe prob=``p``.

    Returns 0.0 when the bet has no edge; clamps at 0.25 to avoid full-
    Kelly blowups. Values outside [0, 1] are coerced.
    """
    p = max(0.0, min(1.0, float(p)))
    mp = max(0.01, min(0.99, float(market_price)))
    if p <= mp:
        return 0.0
    b = (1.0 / mp) - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - p
    f = (p * b - q) / b
    return max(0.0, min(0.25, f))


def bet_size_for(params: dict, bankroll: float, p: float, market_price: float) -> float:
    """Compute the stake for one bet under the configured sizing scheme."""
    strategy = (params.get("bet_sizing") or "flat").lower()
    if strategy == "flat":
        return min(float(params.get("flat_bet_size") or 100), max(0.0, bankroll))
    if strategy == "kelly":
        f = kelly_fraction(p, market_price)
        return max(0.0, bankroll * f)
    if strategy == "proportional_ev":
        ev = max(0.0, p - market_price)
        return max(0.0, bankroll * ev * 0.5)  # 50% of EV fraction
    return 0.0


# ── Sharpe + drawdown ───────────────────────────────────────────────────────


def sharpe_ratio(returns: list[float], *, risk_free: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std <= 0:
        return 0.0
    return round((mean - risk_free) / std, 4)


def max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > worst:
                worst = dd
    return round(worst, 6)


# ── DB access ───────────────────────────────────────────────────────────────


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent / p)
    return Path(__file__).parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _load_predictions(conn: sqlite3.Connection, params: dict) -> list[dict]:
    if not _table_exists(conn, "predictions"):
        return []
    clauses = ["resolved = 1"]
    args: list[Any] = []
    if params.get("date_from"):
        clauses.append("extracted_at >= ?")
        args.append(int(params["date_from"]))
    if params.get("date_to"):
        clauses.append("extracted_at < ?")
        args.append(int(params["date_to"]))
    if params.get("categories"):
        placeholders = ",".join("?" * len(params["categories"]))
        clauses.append(f"category IN ({placeholders})")
        args.extend(params["categories"])
    if params.get("source_handles"):
        placeholders = ",".join("?" * len(params["source_handles"]))
        clauses.append(f"source_handle IN ({placeholders})")
        args.extend(params["source_handles"])

    cred_join = ""
    if _table_exists(conn, "source_credibility"):
        cred_join = "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle"
        if (params.get("min_credibility") or 0) > 0:
            clauses.append("(sc.global_credibility IS NULL OR sc.global_credibility >= ?)")
            args.append(float(params["min_credibility"]))

    sql = (
        "SELECT p.*, "
        + ("sc.global_credibility AS credibility" if cred_join else "0.5 AS credibility") +
        f" FROM predictions p {cred_join} "
        f" WHERE " + " AND ".join(clauses) +
        " ORDER BY p.extracted_at ASC"
    )
    rows = conn.execute(sql, tuple(args)).fetchall()
    return [dict(r) for r in rows]


# ── Engine ──────────────────────────────────────────────────────────────────


def simulate(params: dict, predictions: list[dict]) -> dict:
    """Pure replay — does NOT read the DB. Makes the core fully testable."""
    starting = float(params.get("starting_bankroll") or 10_000.0)
    bankroll = starting
    equity: list[dict] = [{"ts": int(params.get("date_from") or 0), "bankroll": starting}]
    returns: list[float] = []
    bets: list[dict] = []
    wins = 0
    losses = 0

    for pred in predictions:
        if bankroll <= 0:
            break
        p = float(pred.get("predicted_probability") or pred.get("credibility") or 0.5)
        price = float(pred.get("market_price_at_prediction") or pred.get("yes_price") or 0.5)
        ev = max(0.0, p - price)
        if ev < float(params.get("min_ev") or 0):
            continue
        # Hours remaining
        close_time = pred.get("market_close_time")
        extracted = pred.get("extracted_at") or 0
        hours_left = ((close_time or 0) - (extracted or 0)) / 3600.0 if close_time else 999
        if hours_left < float(params.get("min_hours_remaining") or 0):
            continue

        stake = bet_size_for(params, bankroll, p, price)
        if stake <= 0:
            continue

        direction = str(pred.get("direction") or "YES").upper()
        effective_price = price if direction == "YES" else (1.0 - price)
        effective_price = max(0.01, min(0.99, effective_price))
        correct = bool(pred.get("resolved_correct"))

        if correct:
            pnl = stake * (1.0 - effective_price) / effective_price
            wins += 1
        else:
            pnl = -stake
            losses += 1

        bankroll += pnl
        returns.append(pnl / max(stake, 1.0))
        bets.append({
            "market": pred.get("content", "")[:120],
            "direction": direction,
            "stake": round(stake, 2),
            "price": round(price, 4),
            "p_belief": round(p, 4),
            "ev": round(ev, 4),
            "outcome": "win" if correct else "loss",
            "pnl": round(pnl, 2),
            "bankroll_after": round(bankroll, 2),
            "ts": pred.get("extracted_at"),
        })
        equity.append({"ts": pred.get("extracted_at"), "bankroll": round(bankroll, 2)})

    total = wins + losses
    equity_bankrolls = [pt["bankroll"] for pt in equity]
    return {
        "starting_bankroll": starting,
        "final_bankroll": round(bankroll, 2),
        "roi_pct": round((bankroll - starting) / starting * 100.0, 4) if starting else 0.0,
        "win_rate": round(wins / total, 4) if total else 0.0,
        "bet_count": total,
        "wins": wins,
        "losses": losses,
        "sharpe": sharpe_ratio(returns),
        "max_drawdown": max_drawdown(equity_bankrolls),
        "equity_curve": equity,
        "bets": bets[:500],  # cap for payload size
    }


def run_backtest(run_id: int) -> dict:
    """DB-backed wrapper. Called by the async job."""
    conn = _connect()
    try:
        if not _table_exists(conn, "backtest_runs"):
            return {"error": "backtest_runs table missing"}
        row = conn.execute(
            "SELECT * FROM backtest_runs WHERE id = ?", (run_id,),
        ).fetchone()
        if not row:
            return {"error": "backtest not found"}
        try:
            params = json.loads(row["params_json"] or "{}")
        except json.JSONDecodeError:
            params = {}

        conn.execute(
            "UPDATE backtest_runs SET status='running', started_at=? WHERE id=?",
            (int(time.time()), run_id),
        )
        conn.commit()

        predictions = _load_predictions(conn, params)
        result = simulate(params, predictions)
        conn.execute(
            "UPDATE backtest_runs SET status='done', result_json=?, "
            "bet_count=?, final_bankroll=?, roi_pct=?, win_rate=?, "
            "sharpe=?, max_drawdown=?, completed_at=? WHERE id=?",
            (
                json.dumps(result)[:1_000_000],
                result["bet_count"], result["final_bankroll"],
                result["roi_pct"], result["win_rate"],
                result["sharpe"], result["max_drawdown"],
                int(time.time()), run_id,
            ),
        )
        conn.commit()
        return result
    except Exception as exc:
        log.exception("backtest %s failed", run_id)
        conn.execute(
            "UPDATE backtest_runs SET status='failed', error_message=?, completed_at=? WHERE id=?",
            (str(exc)[:500], int(time.time()), run_id),
        )
        conn.commit()
        return {"error": str(exc)}
    finally:
        conn.close()
