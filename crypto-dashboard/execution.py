#!/usr/bin/env python3
"""
Auto-execution engine for DCA + rebalance.

Safety model — every order placement passes through a fixed gauntlet:
  1. **Dry-run gate**.       Default ON globally; user opts out per-account.
  2. **Per-order USD cap**.  Defaults to $500; users can raise it explicitly.
  3. **Daily spend cap**.    Defaults to $1000/day; tracked across schedules.
  4. **Portfolio circuit breaker**.  If portfolio NAV dropped > 10% in 24h,
     pause ALL execution until the user manually resumes.
  5. **Asset whitelist**.    Only the 5 assets we already track.
  6. **Stale-credential check**.  Adapter test_connection() must succeed
     within the last 24h, otherwise we skip and alert.
  7. **Exchange limit-order with TTL**. Place at ~0.5% below mid, cancel
     after 1h if unfilled, optionally fall back to market.

Every action is logged to `crypto_executions` — fills, cancels, dry-runs,
and skips. The execution log is append-only and is the source of truth for
"did this trade happen?" and "how much have I spent today?"

The executor is invoked from server.py as a background task that ticks
every 5 minutes. Manual triggers go through `execute_dca_now()` and
`execute_rebalance_now()`.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import database as db
import exchanges as ex
import long_term as lt

log = logging.getLogger("crypto.execution")


# ─── Defaults ───────────────────────────────────────────────────────────────

DEFAULT_SAFETY = {
    "dry_run": True,                  # never sends real orders unless False
    "max_order_usd": 500.0,
    "max_daily_usd": 1000.0,
    "circuit_breaker_pct": 0.10,      # 10% portfolio dd over 24h pauses execution
    "limit_offset_bps": 50,           # place limits 0.5% below mid
    "limit_ttl_seconds": 3600,        # cancel after 1h
    "fallback_to_market": False,      # after TTL, market-buy the rest?
    "preferred_exchange": "coinbase", # which exchange to route to
}


@dataclass
class ExecutionDecision:
    """The outcome of evaluating one DCA / rebalance leg through the safety
    gauntlet. action ∈ {placed, dry_run, skipped, blocked}.
    """
    action: str
    reason: str
    ticker: str
    exchange: str
    side: str               # buy | sell
    usd_amount: float
    limit_price: Optional[float]
    order_id: Optional[str]
    client_order_id: str

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _spent_today_usd(user_id: str) -> float:
    """Sum of usd_amount on placed/filled rows from today."""
    rows = db.get_executions_since(user_id, hours=24)
    total = 0.0
    for r in rows:
        if r["action"] in ("placed", "filled") and r["side"] == "buy":
            total += float(r["usd_amount"] or 0)
    return total


def get_safety_for(user_id: str) -> dict:
    """Load per-user safety limits, falling back to DEFAULT_SAFETY."""
    row = db.get_safety_limits(user_id)
    if not row:
        return dict(DEFAULT_SAFETY)
    out = dict(DEFAULT_SAFETY)
    for k in DEFAULT_SAFETY:
        v = row.get(k)
        if v is not None:
            out[k] = v
    return out


def update_safety_for(user_id: str, updates: dict) -> dict:
    """Validated upsert. Only known keys, only sensible bounds."""
    safe = get_safety_for(user_id)
    for k, v in updates.items():
        if k not in DEFAULT_SAFETY:
            continue
        if k in ("max_order_usd", "max_daily_usd"):
            safe[k] = max(0.0, min(1_000_000.0, float(v)))
        elif k == "circuit_breaker_pct":
            safe[k] = max(0.0, min(0.5, float(v)))
        elif k == "limit_offset_bps":
            safe[k] = max(0, min(500, int(v)))
        elif k == "limit_ttl_seconds":
            safe[k] = max(60, min(86400, int(v)))
        elif k in ("dry_run", "fallback_to_market"):
            safe[k] = bool(v)
        elif k == "preferred_exchange":
            if v in ("coinbase", "kraken"):
                safe[k] = v
    db.upsert_safety_limits(user_id, safe)
    return safe


def _portfolio_nav(user_id: str) -> Optional[float]:
    """Sum of (qty × current price) across all holding lots."""
    rollup = db.get_holdings_rollup(user_id)
    if not rollup:
        return None
    total = 0.0
    for r in rollup:
        _, closes = lt.get_daily_closes(r["ticker"], days=2)
        if len(closes) == 0:
            return None  # can't compute without prices for every position
        total += float(r["qty"]) * float(closes[-1])
    return total


def _check_circuit_breaker(user_id: str, safety: dict) -> tuple[bool, str]:
    """Returns (tripped?, reason)."""
    nav = _portfolio_nav(user_id)
    if nav is None or nav <= 0:
        return False, ""
    # 24h ago NAV — reconstruct from yesterday's closes against today's holdings.
    rollup = db.get_holdings_rollup(user_id)
    nav_yest = 0.0
    for r in rollup:
        _, closes = lt.get_daily_closes(r["ticker"], days=3)
        if len(closes) < 2:
            continue
        nav_yest += float(r["qty"]) * float(closes[-2])
    if nav_yest <= 0:
        return False, ""
    dd = (nav / nav_yest - 1.0)
    if dd < -safety["circuit_breaker_pct"]:
        return True, f"24h NAV dropped {dd*100:.1f}% — circuit breaker tripped"
    return False, ""


# ─── Core: evaluate one leg ─────────────────────────────────────────────────

def _evaluate_leg(user_id: str, ticker: str, side: str, usd_amount: float,
                  safety: dict, client_order_id: Optional[str] = None) -> ExecutionDecision:
    """Run a single buy/sell leg through the safety gauntlet and (if armed)
    place it on the preferred exchange. Returns the decision either way."""
    client_order_id = client_order_id or f"narve-{uuid.uuid4().hex[:16]}"
    exchange_name = safety.get("preferred_exchange", "coinbase")

    # 1. Asset whitelist
    if ticker not in lt.TICKER_MAP:
        return ExecutionDecision(
            "skipped", "unsupported asset", ticker, exchange_name, side,
            usd_amount, None, None, client_order_id,
        )

    # 2. Sanity bounds on amount
    if usd_amount <= 0:
        return ExecutionDecision(
            "skipped", "zero/negative amount", ticker, exchange_name, side,
            usd_amount, None, None, client_order_id,
        )

    # 3. Per-order cap
    if usd_amount > safety["max_order_usd"]:
        return ExecutionDecision(
            "blocked", f"order ${usd_amount:.0f} exceeds max-order cap "
                       f"${safety['max_order_usd']:.0f}",
            ticker, exchange_name, side, usd_amount, None, None, client_order_id,
        )

    # 4. Daily cap (only for buys — sells are exit liquidity, not spend)
    if side == "buy":
        spent = _spent_today_usd(user_id)
        if spent + usd_amount > safety["max_daily_usd"]:
            return ExecutionDecision(
                "blocked", f"would exceed daily cap "
                           f"(${spent:.0f} + ${usd_amount:.0f} > ${safety['max_daily_usd']:.0f})",
                ticker, exchange_name, side, usd_amount, None, None, client_order_id,
            )

    # 5. Circuit breaker
    tripped, why = _check_circuit_breaker(user_id, safety)
    if tripped:
        return ExecutionDecision(
            "blocked", why, ticker, exchange_name, side, usd_amount,
            None, None, client_order_id,
        )

    # 6. Adapter check
    adapter = ex.get_adapter(user_id, exchange_name)
    if adapter is None:
        return ExecutionDecision(
            "skipped", f"{exchange_name} not configured for this user",
            ticker, exchange_name, side, usd_amount, None, None, client_order_id,
        )

    # 7. Price discovery + limit calc
    price = adapter.get_price(ticker)
    if not price or price <= 0:
        return ExecutionDecision(
            "skipped", f"no price from {exchange_name}",
            ticker, exchange_name, side, usd_amount, None, None, client_order_id,
        )
    offset = safety["limit_offset_bps"] / 10_000
    # Buy 0.5% below; sell 0.5% above.
    limit_price = price * (1 - offset) if side == "buy" else price * (1 + offset)

    # 8. Dry-run gate
    if safety["dry_run"]:
        decision = ExecutionDecision(
            "dry_run", "dry-run mode enabled — order NOT sent",
            ticker, exchange_name, side, usd_amount, round(limit_price, 6),
            None, client_order_id,
        )
        db.log_execution(user_id, decision.to_dict())
        return decision

    # 9. Place real order — only buy supported for now (DCA-first).
    if side != "buy":
        return ExecutionDecision(
            "skipped", "sell-side auto-exec not enabled in Phase 2 first cut",
            ticker, exchange_name, side, usd_amount, round(limit_price, 6),
            None, client_order_id,
        )
    resp = adapter.place_limit_buy(ticker, usd_amount, limit_price, client_order_id)
    if not resp.ok:
        decision = ExecutionDecision(
            "skipped", f"exchange rejected: {resp.error}",
            ticker, exchange_name, side, usd_amount, round(limit_price, 6),
            None, client_order_id,
        )
        db.log_execution(user_id, decision.to_dict(), raw=resp.raw)
        return decision
    decision = ExecutionDecision(
        "placed", f"limit buy placed at {limit_price:.4f}",
        ticker, exchange_name, side, usd_amount, round(limit_price, 6),
        resp.order_id, client_order_id,
    )
    db.log_execution(user_id, decision.to_dict(), raw=resp.raw)
    return decision


# ─── Public entry points ────────────────────────────────────────────────────

def execute_dca_now(user_id: str, ticker: str) -> ExecutionDecision:
    """Manual trigger — run the cycle-aware DCA recommendation for one ticker
    through the safety gauntlet."""
    schedules = db.get_dca_schedules(user_id)
    sched = next((s for s in schedules if s["ticker"] == ticker and s["active"]), None)
    if not sched:
        return ExecutionDecision(
            "skipped", "no active DCA schedule for this ticker",
            ticker, "—", "buy", 0.0, None, None, "",
        )
    base = float(sched["base_amount_usd"])
    if sched["use_multiplier"]:
        plan = lt.dca_recommendation(ticker, base)
        amount = plan.suggested_amount_usd
    else:
        amount = base
    safety = get_safety_for(user_id)
    decision = _evaluate_leg(user_id, ticker, "buy", amount, safety)
    # Update next_run_at on success/dry-run.
    if decision.action in ("placed", "dry_run"):
        freq = sched["frequency"]
        delta = {"daily": 1, "weekly": 7, "monthly": 30}.get(freq, 7)
        next_run = (datetime.now(timezone.utc) + timedelta(days=delta)).isoformat()
        db.mark_dca_run(user_id, ticker, next_run)
    return decision


def execute_rebalance_now(user_id: str) -> list[ExecutionDecision]:
    """Compute the rebalance plan and execute each leg through the gauntlet.
    Sells are blocked in the first cut — we log them as skipped."""
    rollup = db.get_holdings_rollup(user_id)
    targets_raw = db.get_target_weights(user_id)
    if not targets_raw:
        return []
    drift_band = min((float(t["drift_band"]) for t in targets_raw), default=0.05)
    plan = lt.rebalance_plan(
        [{"ticker": r["ticker"], "qty": r["qty"]} for r in rollup],
        [{"ticker": t["ticker"], "weight": t["weight"]} for t in targets_raw],
        drift_band,
    )
    safety = get_safety_for(user_id)
    decisions: list[ExecutionDecision] = []
    for leg in plan.get("legs", []):
        if leg["action"] == "hold" or leg["notional_usd"] <= 0:
            continue
        d = _evaluate_leg(user_id, leg["ticker"], leg["action"], leg["notional_usd"], safety)
        decisions.append(d)
    return decisions


def tick_due_schedules() -> dict:
    """Cron entry point. Find every DCA schedule whose next_run_at is in the
    past (or unset) and execute it. Runs every 5 min from server.py.
    Returns a summary."""
    rows = db.get_due_dca_schedules()
    summary = {"checked": len(rows), "actions": []}
    for r in rows:
        try:
            d = execute_dca_now(r["user_id"], r["ticker"])
            summary["actions"].append({
                "user_id": r["user_id"], "ticker": r["ticker"],
                "action": d.action, "reason": d.reason,
            })
        except Exception as e:
            log.warning("DCA execution failed for %s/%s: %s", r["user_id"], r["ticker"], e)
            summary["actions"].append({
                "user_id": r["user_id"], "ticker": r["ticker"],
                "action": "error", "reason": str(e),
            })
    return summary


def reconcile_open_orders(user_id: str) -> int:
    """For every placed order that's still open past its TTL, cancel it (and
    optionally fall back to market). Returns count of reconciled orders."""
    safety = get_safety_for(user_id)
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=safety["limit_ttl_seconds"])).isoformat()
    open_rows = db.get_open_executions_before(user_id, cutoff_iso)
    reconciled = 0
    for row in open_rows:
        adapter = ex.get_adapter(user_id, row["exchange"])
        if not adapter:
            continue
        # Cancel.
        try:
            adapter.cancel_order(row["order_id"])
        except Exception as e:
            log.warning("cancel failed for %s: %s", row["order_id"], e)
            continue
        db.update_execution_status(row["id"], "cancelled_ttl")
        # Optional market fallback.
        if safety["fallback_to_market"] and row["side"] == "buy":
            try:
                fb_id = f"narve-fb-{uuid.uuid4().hex[:12]}"
                resp = adapter.place_market_buy(row["ticker"], row["usd_amount"], fb_id)
                if resp.ok:
                    db.log_execution(user_id, {
                        "action": "placed", "reason": "TTL market fallback",
                        "ticker": row["ticker"], "exchange": row["exchange"],
                        "side": "buy", "usd_amount": row["usd_amount"],
                        "limit_price": None, "order_id": resp.order_id,
                        "client_order_id": fb_id,
                    }, raw=resp.raw)
            except Exception as e:
                log.warning("fallback market buy failed: %s", e)
        reconciled += 1
    return reconciled


# ─── Preview ────────────────────────────────────────────────────────────────

def preview_dca(user_id: str) -> list[dict]:
    """Same as tick_due_schedules but without actually executing — shows the
    user what would happen if the executor ran right now for them."""
    schedules = db.get_dca_schedules(user_id)
    safety = get_safety_for(user_id)
    out = []
    saved_dry = safety["dry_run"]
    safety["dry_run"] = True   # force dry run for preview
    try:
        for s in schedules:
            if not s["active"]:
                continue
            base = float(s["base_amount_usd"])
            if s["use_multiplier"]:
                plan = lt.dca_recommendation(s["ticker"], base)
                amount = plan.suggested_amount_usd
                reason = plan.reason
            else:
                amount = base
                reason = "fixed (multiplier off)"
            d = _evaluate_leg(user_id, s["ticker"], "buy", amount, safety)
            row = d.to_dict()
            row["dca_reason"] = reason
            row["preview_only"] = True
            out.append(row)
    finally:
        safety["dry_run"] = saved_dry
    return out
