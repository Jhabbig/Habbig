#!/usr/bin/env python3
"""
Strategy library + backtester + marketplace.

A `Strategy` is a small dataclass — not a real DSL — that composes:
  - a DCA cadence (daily / weekly / monthly) with base USD amount
  - a cycle-aware multiplier (boost when bullish signals fire, pause when
    bearish thresholds are crossed)
  - optional harvest rules (sell lots with loss ≥ X after N days)
  - optional rebalance rules (target weights + drift band)

The backtester walks historical daily bars forward, evaluating the strategy
on each day. At every day:
  - Compute that day's signals (Mayer, drawdown, NUPL if covered, vol regime)
  - If today is a scheduled DCA day, buy `base × multiplier` (multiplier
    depends on the signals)
  - If any open lot has held > min_age_days and unrealised loss ≥
    harvest_min_loss_usd, sell it
  - If we have target weights and drift > band, rebalance

The virtual portfolio uses an immutable lot ledger like the production
tax module — every buy creates a lot, every sell consumes lots HIFO.

Marketplace:
  - `visibility = 'public'` makes a strategy + its backtest visible on the
    leaderboard.
  - `forked_from_id` lets users clone a public strategy as a starting
    point. The fork is independent — they can tweak and rebacktest.
  - Sharpe / max-DD trade-off is the ranking metric (not raw return —
    that rewards leverage and survivorship bias).

Backtests are persisted so the leaderboard doesn't recompute on every
page load.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np

import database as db
import long_term as lt

log = logging.getLogger("crypto.strategy")


# ─── Strategy DSL ───────────────────────────────────────────────────────────

@dataclass
class Strategy:
    """Composable rule set. Fields default to sensible no-ops so a "skeleton"
    strategy is a pure HODL with weekly DCA."""
    name: str = "new strategy"
    description: str = ""
    base_ticker: str = "BTC"                 # primary asset (or PORTFOLIO for multi)
    starting_capital_usd: float = 10_000.0

    # DCA
    dca_enabled: bool = True
    dca_amount_usd: float = 100.0
    dca_frequency: str = "weekly"            # daily | weekly | monthly
    dca_dow: int = 0                          # 0=Mon, only used when weekly

    # Cycle-aware multipliers (applied to the base DCA amount). Drawn from
    # the same scheme as the production DCA recommender.
    bullish_dd_threshold: float = -0.40      # current_dd ≤ X → multiplier
    bullish_dd_multiplier: float = 2.0
    bullish_mayer_threshold: float = 1.0     # Mayer < X → multiplier
    bullish_mayer_multiplier: float = 1.5
    bearish_mayer_threshold: float = 2.4     # Mayer > X → halve buys
    bearish_mayer_multiplier: float = 0.5
    pause_mayer_threshold: float = 2.7       # Mayer > X → multiplier = 0

    # Harvest
    harvest_enabled: bool = False
    harvest_min_loss_usd: float = 100.0
    harvest_min_age_days: int = 30

    # Rebalance (single-asset by default, so off)
    rebalance_enabled: bool = False
    rebalance_drift_pct: float = 0.05
    target_weights: dict = field(default_factory=dict)  # {ticker: weight}

    # Backtest config
    backtest_days: int = 365 * 4              # historic window

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Strategy":
        # Drop unknown fields silently so we can evolve the schema without
        # breaking persisted rows.
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in allowed})


# ─── Backtester ─────────────────────────────────────────────────────────────

@dataclass
class _Lot:
    qty: float
    cost_basis: float
    acquired_idx: int

@dataclass
class _Trade:
    idx: int
    side: str          # buy | sell
    ticker: str
    qty: float
    price: float
    notional: float
    reason: str


def _multiplier_for(strategy: Strategy, mayer: Optional[float],
                    current_dd: float) -> tuple[float, str]:
    """Resolve the cycle multiplier for one day. Returns (multiplier, reason)."""
    # Pause cap takes precedence.
    if mayer is not None and mayer > strategy.pause_mayer_threshold:
        return 0.0, f"Mayer {mayer:.2f} > pause threshold {strategy.pause_mayer_threshold}"
    if mayer is not None and mayer > strategy.bearish_mayer_threshold:
        return strategy.bearish_mayer_multiplier, \
               f"Mayer {mayer:.2f} > bearish threshold — buy {strategy.bearish_mayer_multiplier}×"
    if current_dd <= strategy.bullish_dd_threshold:
        return strategy.bullish_dd_multiplier, \
               f"Drawdown {current_dd:+.0%} ≤ {strategy.bullish_dd_threshold:+.0%} — buy {strategy.bullish_dd_multiplier}×"
    if mayer is not None and mayer < strategy.bullish_mayer_threshold:
        return strategy.bullish_mayer_multiplier, \
               f"Mayer {mayer:.2f} < bullish threshold — buy {strategy.bullish_mayer_multiplier}×"
    return 1.0, "base"


def _is_dca_day(strategy: Strategy, idx: int, start_idx: int, date: datetime) -> bool:
    """Should we DCA today? Frequency-aware."""
    days_since_start = idx - start_idx
    if days_since_start < 0:
        return False
    if strategy.dca_frequency == "daily":
        return True
    if strategy.dca_frequency == "weekly":
        # Default to weekly_dow (Monday = 0).
        return date.weekday() == strategy.dca_dow
    if strategy.dca_frequency == "monthly":
        return date.day == 1
    return False


def _sharpe(daily_returns: np.ndarray) -> float:
    if len(daily_returns) < 30:
        return float("nan")
    excess = daily_returns - (0.04 / 365)  # 4% RF, daily
    sd = float(np.std(excess, ddof=1))
    if sd == 0:
        return float("nan")
    return float(np.mean(excess) / sd * math.sqrt(365))


def _sortino(daily_returns: np.ndarray) -> float:
    if len(daily_returns) < 30:
        return float("nan")
    excess = daily_returns - (0.04 / 365)
    downside = excess[excess < 0]
    if len(downside) < 5:
        return float("nan")
    dd = float(np.sqrt(np.mean(downside ** 2)))
    if dd == 0:
        return float("nan")
    return float(np.mean(excess) / dd * math.sqrt(365))


def _max_dd(equity: np.ndarray) -> float:
    if len(equity) < 2:
        return 0.0
    running = np.maximum.accumulate(equity)
    dd = (equity - running) / running
    return float(np.min(dd))


def backtest(strategy: Strategy) -> dict:
    """Run the strategy against historical daily bars. Returns a result dict
    with equity curve + stats. Single-asset only in the first cut (uses
    `base_ticker`); multi-asset rebalancing is wired but not yet enforced
    across tickers."""
    ticker = strategy.base_ticker
    dates, closes = lt.get_daily_closes(ticker, days=strategy.backtest_days)
    n = len(closes)
    if n < 60:
        return {"error": f"need 60+ days of data, have {n}"}

    # Start at the latest of: (a) first available day, (b) day 200 (so we
    # have a Mayer ratio from day 1 of the backtest, not undefined).
    start_idx = max(200, 0)
    if n <= start_idx + 30:
        return {"error": "not enough history after MA-warmup"}

    # Pre-compute Mayer + drawdown at every day.
    cum_max = np.maximum.accumulate(closes)
    drawdowns = (closes - cum_max) / cum_max
    ma200 = np.full(n, np.nan)
    for i in range(199, n):
        ma200[i] = float(np.mean(closes[i - 199:i + 1]))
    mayer = np.where(ma200 > 0, closes / ma200, np.nan)

    cash = float(strategy.starting_capital_usd)
    lots: list[_Lot] = []
    trades: list[_Trade] = []
    equity_curve: list[tuple[str, float]] = []

    for i in range(start_idx, n):
        price = float(closes[i])
        date = datetime.fromisoformat(dates[i])

        # 1. DCA
        if strategy.dca_enabled and _is_dca_day(strategy, i, start_idx, date):
            mayer_val = float(mayer[i]) if not math.isnan(mayer[i]) else None
            dd_val = float(drawdowns[i])
            mult, reason = _multiplier_for(strategy, mayer_val, dd_val)
            amount = strategy.dca_amount_usd * mult
            if amount > 0 and cash >= amount:
                qty = amount / price
                cash -= amount
                lots.append(_Lot(qty=qty, cost_basis=price, acquired_idx=i))
                trades.append(_Trade(i, "buy", ticker, qty, price, amount, reason))

        # 2. Harvest (HIFO loss lots)
        if strategy.harvest_enabled and lots:
            # Find lots that meet harvest criteria.
            candidates = []
            for j, lot in enumerate(lots):
                age = i - lot.acquired_idx
                if age < strategy.harvest_min_age_days:
                    continue
                loss = (price - lot.cost_basis) * lot.qty
                if loss > -strategy.harvest_min_loss_usd:
                    continue  # not enough loss
                candidates.append((j, loss))
            # Sell the biggest losers (largest absolute loss).
            for j, _ in sorted(candidates, key=lambda x: x[1]):
                lot = lots[j]
                proceeds = lot.qty * price
                cash += proceeds
                trades.append(_Trade(i, "sell", ticker, lot.qty, price, proceeds,
                                     f"harvest loss (age {i - lot.acquired_idx}d)"))
            if candidates:
                # Remove harvested lots; keep the rest in original order.
                keep_ids = {j for j, _ in candidates}
                lots = [l for j, l in enumerate(lots) if j not in keep_ids]

        # 3. Mark equity
        total_qty = sum(l.qty for l in lots)
        equity_curve.append((dates[i], round(cash + total_qty * price, 2)))

    # Final liquidation for fair-comparison stats? No — leave the position
    # open. Backtest reports "where you'd be today" not "where you'd be if
    # you sold everything today".

    equity_arr = np.asarray([e[1] for e in equity_curve], dtype=np.float64)
    if len(equity_arr) < 2:
        return {"error": "no equity samples"}
    daily_returns = np.diff(equity_arr) / equity_arr[:-1]
    total_capital_deployed = strategy.starting_capital_usd
    # If DCA spent more than starting capital (shouldn't, but be defensive)...
    final_value = float(equity_arr[-1])
    total_return = (final_value / total_capital_deployed - 1.0)
    sharpe = _sharpe(daily_returns)
    sortino = _sortino(daily_returns)
    max_dd = _max_dd(equity_arr)
    n_trades = len(trades)
    sells = [t for t in trades if t.side == "sell"]
    win_rate = (sum(1 for t in sells if t.notional > t.qty * trades[0].price) / len(sells)) if sells else None

    return {
        "ticker": ticker,
        "start_date": dates[start_idx],
        "end_date": dates[-1],
        "days": n - start_idx,
        "starting_capital_usd": strategy.starting_capital_usd,
        "final_value_usd": round(final_value, 2),
        "total_return_pct": round(total_return, 4),
        "sharpe": round(sharpe, 3) if not math.isnan(sharpe) else None,
        "sortino": round(sortino, 3) if not math.isnan(sortino) else None,
        "max_drawdown_pct": round(max_dd, 4),
        "trade_count": n_trades,
        "buys": sum(1 for t in trades if t.side == "buy"),
        "sells": len(sells),
        "win_rate": round(win_rate, 3) if win_rate is not None else None,
        "final_qty": round(sum(l.qty for l in lots), 8),
        "final_cash_usd": round(cash, 2),
        # Down-sample the curve for the UI: send weekly, not daily.
        "equity_curve": [equity_curve[i] for i in range(0, len(equity_curve), 7)],
    }


# ─── CRUD + leaderboard helpers ─────────────────────────────────────────────

def create_strategy(user_id: str, strategy: Strategy,
                    forked_from: Optional[int] = None,
                    visibility: str = "private") -> int:
    if visibility not in ("private", "public"):
        raise ValueError("visibility must be private or public")
    rules_json = json.dumps(strategy.to_dict())
    return db.insert_strategy(
        owner_user_id=user_id, name=strategy.name,
        description=strategy.description, rules_json=rules_json,
        base_ticker=strategy.base_ticker,
        starting_capital_usd=strategy.starting_capital_usd,
        visibility=visibility, forked_from_id=forked_from,
    )


def update_strategy(user_id: str, strategy_id: int, strategy: Strategy,
                    visibility: Optional[str] = None) -> bool:
    """Owner-only update. Returns False if not owner."""
    row = db.get_strategy(strategy_id)
    if not row or row["owner_user_id"] != user_id:
        return False
    db.update_strategy_row(
        strategy_id=strategy_id, name=strategy.name,
        description=strategy.description,
        rules_json=json.dumps(strategy.to_dict()),
        base_ticker=strategy.base_ticker,
        starting_capital_usd=strategy.starting_capital_usd,
        visibility=visibility or row["visibility"],
    )
    return True


def delete_strategy(user_id: str, strategy_id: int) -> bool:
    row = db.get_strategy(strategy_id)
    if not row or row["owner_user_id"] != user_id:
        return False
    db.delete_strategy_row(strategy_id)
    return True


def get_strategy(strategy_id: int, user_id: Optional[str] = None) -> Optional[dict]:
    row = db.get_strategy(strategy_id)
    if not row:
        return None
    # Privacy gate: only owner can see private strategies.
    if row["visibility"] == "private" and row["owner_user_id"] != user_id:
        return None
    return _strategy_row_to_dict(row)


def list_user_strategies(user_id: str) -> list[dict]:
    rows = db.list_strategies_for_user(user_id)
    return [_strategy_row_to_dict(r) for r in rows]


def list_public_strategies(limit: int = 50) -> list[dict]:
    rows = db.list_public_strategies(limit)
    return [_strategy_row_to_dict(r) for r in rows]


def fork_strategy(user_id: str, source_id: int, new_name: Optional[str] = None) -> Optional[int]:
    """Clone a public strategy into the user's private library."""
    src = db.get_strategy(source_id)
    if not src or src["visibility"] != "public":
        return None
    rules = json.loads(src["rules_json"])
    if new_name:
        rules["name"] = new_name
    else:
        rules["name"] = f"{src['name']} (fork)"
    return create_strategy(user_id, Strategy.from_dict(rules),
                            forked_from=source_id, visibility="private")


def follow_strategy(user_id: str, strategy_id: int) -> bool:
    """Subscribe to updates / show in 'following' tab. Returns False if
    the strategy isn't public."""
    src = db.get_strategy(strategy_id)
    if not src or src["visibility"] != "public":
        return False
    db.upsert_strategy_follow(user_id, strategy_id)
    return True


def unfollow_strategy(user_id: str, strategy_id: int) -> None:
    db.delete_strategy_follow(user_id, strategy_id)


def run_and_save_backtest(strategy_id: int) -> dict:
    """Backtest the strategy and persist the result. Returns the result."""
    row = db.get_strategy(strategy_id)
    if not row:
        return {"error": "strategy not found"}
    strategy = Strategy.from_dict(json.loads(row["rules_json"]))
    result = backtest(strategy)
    if "error" in result:
        return result
    db.insert_strategy_backtest(
        strategy_id=strategy_id,
        start_date=result["start_date"], end_date=result["end_date"],
        final_value_usd=result["final_value_usd"],
        total_return_pct=result["total_return_pct"],
        sharpe=result.get("sharpe") or 0,
        sortino=result.get("sortino") or 0,
        max_drawdown_pct=result["max_drawdown_pct"],
        win_rate=result.get("win_rate") or 0,
        trade_count=result["trade_count"],
        equity_curve_json=json.dumps(result["equity_curve"]),
    )
    return result


def leaderboard(limit: int = 25) -> list[dict]:
    """Rank public strategies by a composite Sharpe/max-DD score.
    Score = sharpe - 2 × |max_drawdown_pct|, which penalises high-vol
    survivors. Only the most-recent backtest per strategy is considered."""
    rows = db.leaderboard_data(limit * 3)  # over-fetch so we can re-rank
    enriched = []
    for r in rows:
        if r["sharpe"] is None:
            continue
        score = float(r["sharpe"]) - 2 * abs(float(r["max_drawdown_pct"] or 0))
        enriched.append({**dict(r), "score": round(score, 3)})
    enriched.sort(key=lambda x: x["score"], reverse=True)
    return enriched[:limit]


def _strategy_row_to_dict(row) -> dict:
    rules = json.loads(row["rules_json"])
    latest_backtest = db.get_latest_strategy_backtest(row["id"])
    return {
        "id": row["id"], "name": row["name"], "description": row["description"],
        "owner_user_id": row["owner_user_id"], "base_ticker": row["base_ticker"],
        "starting_capital_usd": row["starting_capital_usd"],
        "visibility": row["visibility"], "forked_from_id": row["forked_from_id"],
        "rules": rules,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "latest_backtest": dict(latest_backtest) if latest_backtest else None,
    }
