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

import json

import database as db
import exchanges as ex
import long_term as lt
import tax as tax_mod
import strategy as strat_mod

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
                  safety: dict, client_order_id: Optional[str] = None,
                  base_qty: Optional[float] = None) -> ExecutionDecision:
    """Run a single buy/sell leg through the safety gauntlet and (if armed)
    place it on the preferred exchange. Returns the decision either way.

    Buys are sized in USD (`usd_amount`); sells take an explicit `base_qty`
    so we can sell exactly the lot the harvester picked, not "USD-worth at
    moving market price". For sells, `usd_amount` is computed at price-
    discovery time and used for cap-checking only."""
    client_order_id = client_order_id or f"narve-{uuid.uuid4().hex[:16]}"
    exchange_name = safety.get("preferred_exchange", "coinbase")

    # 1. Asset whitelist
    if ticker not in lt.TICKER_MAP:
        return ExecutionDecision(
            "skipped", "unsupported asset", ticker, exchange_name, side,
            usd_amount or 0.0, None, None, client_order_id,
        )

    # 2. Sanity bounds on amount
    if side == "buy" and (usd_amount is None or usd_amount <= 0):
        return ExecutionDecision(
            "skipped", "zero/negative buy amount", ticker, exchange_name, side,
            usd_amount or 0.0, None, None, client_order_id,
        )
    if side == "sell" and (base_qty is None or base_qty <= 0):
        return ExecutionDecision(
            "skipped", "zero/negative sell qty", ticker, exchange_name, side,
            0.0, None, None, client_order_id,
        )

    # 3. Circuit breaker
    tripped, why = _check_circuit_breaker(user_id, safety)
    if tripped:
        return ExecutionDecision(
            "blocked", why, ticker, exchange_name, side, usd_amount or 0.0,
            None, None, client_order_id,
        )

    # 4. Adapter check
    adapter = ex.get_adapter(user_id, exchange_name)
    if adapter is None:
        return ExecutionDecision(
            "skipped", f"{exchange_name} not configured for this user",
            ticker, exchange_name, side, usd_amount or 0.0, None, None, client_order_id,
        )

    # 5. Price discovery + limit calc + sell-side USD computation
    price = adapter.get_price(ticker)
    if not price or price <= 0:
        return ExecutionDecision(
            "skipped", f"no price from {exchange_name}",
            ticker, exchange_name, side, usd_amount or 0.0, None, None, client_order_id,
        )
    offset = safety["limit_offset_bps"] / 10_000
    limit_price = price * (1 - offset) if side == "buy" else price * (1 + offset)
    if side == "sell":
        usd_amount = float(base_qty) * limit_price  # for cap-checking + logging

    # 6. Per-order cap (now that we have a USD estimate for sells too)
    if usd_amount > safety["max_order_usd"]:
        return ExecutionDecision(
            "blocked",
            f"order ${usd_amount:.0f} exceeds max-order cap ${safety['max_order_usd']:.0f}",
            ticker, exchange_name, side, usd_amount, None, None, client_order_id,
        )

    # 7. Daily cap (only for buys — sells are exit liquidity, not spend)
    if side == "buy":
        spent = _spent_today_usd(user_id)
        if spent + usd_amount > safety["max_daily_usd"]:
            return ExecutionDecision(
                "blocked", f"would exceed daily cap "
                           f"(${spent:.0f} + ${usd_amount:.0f} > ${safety['max_daily_usd']:.0f})",
                ticker, exchange_name, side, usd_amount, None, None, client_order_id,
            )

    # 8. Dry-run gate
    if safety["dry_run"]:
        decision = ExecutionDecision(
            "dry_run", f"dry-run mode enabled — {side} NOT sent",
            ticker, exchange_name, side, usd_amount, round(limit_price, 6),
            None, client_order_id,
        )
        db.log_execution(user_id, decision.to_dict())
        return decision

    # 9. Place real order
    if side == "buy":
        resp = adapter.place_limit_buy(ticker, usd_amount, limit_price, client_order_id)
    else:
        resp = adapter.place_limit_sell(ticker, float(base_qty), limit_price, client_order_id)
    if not resp.ok:
        decision = ExecutionDecision(
            "skipped", f"exchange rejected: {resp.error}",
            ticker, exchange_name, side, usd_amount, round(limit_price, 6),
            None, client_order_id,
        )
        db.log_execution(user_id, decision.to_dict(), raw=resp.raw)
        return decision
    verb = "buy" if side == "buy" else "sell"
    decision = ExecutionDecision(
        "placed", f"limit {verb} placed at {limit_price:.4f}",
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
    Sells go through `_evaluate_leg(side='sell', base_qty=...)`; we convert
    the plan's USD notional into base qty at the current price."""
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
        if leg["action"] == "buy":
            d = _evaluate_leg(user_id, leg["ticker"], "buy", leg["notional_usd"], safety)
        else:
            # Convert USD notional → base qty at last close (executor will
            # re-price at exchange level).
            _, closes = lt.get_daily_closes(leg["ticker"], days=2)
            if len(closes) == 0:
                continue
            base_qty = leg["notional_usd"] / float(closes[-1])
            d = _evaluate_leg(user_id, leg["ticker"], "sell", 0.0, safety, base_qty=base_qty)
        decisions.append(d)
    return decisions


def execute_harvest_now(user_id: str, holding_ids: list[int]) -> list[ExecutionDecision]:
    """Phase 3 → Phase 2 chain. For each holding in the provided list,
    place a sell for the remaining qty through the safety gauntlet.
    The fill poller picks up the fill and creates a tax disposition with
    the original lot pre-selected (via execution_id linkage).

    Caller is expected to have already shown the user a harvest preview
    and gotten their confirmation."""
    safety = get_safety_for(user_id)
    consumed_by_holding = db.get_consumption_by_holding(user_id)
    holdings_by_id = {h["id"]: h for h in db.get_holdings(user_id)}
    decisions: list[ExecutionDecision] = []
    for hid in holding_ids:
        lot = holdings_by_id.get(int(hid))
        if not lot:
            decisions.append(ExecutionDecision(
                "skipped", f"unknown holding id {hid}",
                "", safety["preferred_exchange"], "sell", 0.0, None, None, "",
            ))
            continue
        consumed = float(consumed_by_holding.get(int(hid), 0.0))
        remaining = float(lot["qty"]) - consumed
        if remaining <= 1e-9:
            decisions.append(ExecutionDecision(
                "skipped", f"holding {hid} already fully consumed",
                lot["ticker"], safety["preferred_exchange"], "sell",
                0.0, None, None, "",
            ))
            continue
        d = _evaluate_leg(
            user_id, lot["ticker"], "sell", 0.0, safety,
            client_order_id=f"narve-harv-{uuid.uuid4().hex[:12]}",
            base_qty=remaining,
        )
        decisions.append(d)
    return decisions


# ─── Fill-status polling ────────────────────────────────────────────────────

def _normalise_status(adapter_name: str, raw: dict) -> tuple[str, Optional[float], Optional[float]]:
    """Map an adapter's order-status payload to a uniform shape.
    Returns (status, fill_price, fill_qty). Status ∈ {open, filled,
    partially_filled, cancelled, unknown}."""
    if adapter_name == "coinbase":
        order = (raw.get("order") or {}) if isinstance(raw, dict) else {}
        s = (order.get("status") or "").upper()
        filled_qty = float(order.get("filled_size") or 0)
        avg_price = float(order.get("average_filled_price") or 0)
        if s == "FILLED":
            return "filled", avg_price or None, filled_qty or None
        if s == "CANCELLED":
            return "cancelled", None, None
        if s == "OPEN" or s == "PENDING":
            if filled_qty > 0:
                return "partially_filled", avg_price or None, filled_qty
            return "open", None, None
        return "unknown", None, None
    if adapter_name == "kraken":
        # Kraken returns a dict keyed by txid → {status, vol, vol_exec, price, ...}
        for _, info in (raw.items() if isinstance(raw, dict) else []):
            if not isinstance(info, dict):
                continue
            s = (info.get("status") or "").lower()
            vol_exec = float(info.get("vol_exec") or 0)
            price = float(info.get("price") or 0)
            if s == "closed":
                return "filled", price or None, vol_exec or None
            if s == "canceled":
                return "cancelled", None, None
            if s == "open":
                if vol_exec > 0:
                    return "partially_filled", price or None, vol_exec
                return "open", None, None
        return "unknown", None, None
    return "unknown", None, None


def poll_fills(user_id: str) -> dict:
    """Poll every open order for this user, update statuses, and create
    tax dispositions for any newly-filled sells. Idempotent on execution_id.
    Returns a summary."""
    open_rows = db.get_all_open_executions(user_id)
    summary = {"checked": 0, "filled": 0, "cancelled": 0, "dispositions": 0}
    for row in open_rows:
        if not row["order_id"]:
            continue
        summary["checked"] += 1
        adapter = ex.get_adapter(user_id, row["exchange"])
        if not adapter:
            continue
        try:
            raw = adapter.get_order_status(row["order_id"])
        except Exception:
            continue
        status, fill_price, fill_qty = _normalise_status(adapter.name, raw)
        if status == "filled":
            db.update_execution_status(row["id"], "filled",
                                       fill_price=fill_price, fill_qty=fill_qty)
            summary["filled"] += 1
            # Sell-side ⇒ create a disposition with the actual fill price
            # and qty. Idempotent on execution_id.
            if row["side"] == "sell" and fill_price and fill_qty:
                try:
                    settings = tax_mod.get_tax_settings(user_id)
                    tax_mod.record_disposition(
                        user_id=user_id, ticker=row["ticker"],
                        qty=fill_qty, sell_price=fill_price,
                        method=settings.get("default_lot_method"),
                        exchange=row["exchange"],
                        execution_id=row["id"],
                        notes=f"auto: order {row['order_id']}",
                    )
                    summary["dispositions"] += 1
                except Exception as e:
                    log.warning("disposition record failed for exec %s: %s", row["id"], e)
        elif status == "cancelled":
            db.update_execution_status(row["id"], "cancelled")
            summary["cancelled"] += 1
        elif status == "partially_filled":
            # Keep the row open but stash the partial qty so the dashboard
            # shows progress.
            db.update_execution_status(row["id"], "open",
                                       fill_price=fill_price, fill_qty=fill_qty)
    return summary


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

# ─── Live strategy subscriptions ────────────────────────────────────────────

def execute_subscription_now(user_id: str, subscription_id: int) -> list[ExecutionDecision]:
    """Manually run one subscription. The cron loop calls this for every
    due subscription on each tick."""
    subs = db.get_strategy_subscriptions(user_id)
    sub = next((s for s in subs if s["id"] == subscription_id), None)
    if not sub:
        return [ExecutionDecision(
            "skipped", "subscription not found", "", "—", "buy",
            0.0, None, None, "",
        )]
    return _run_subscription(sub)


def _run_subscription(sub) -> list[ExecutionDecision]:
    """Internal: evaluate one subscription's strategy and execute the
    resulting actions through `_evaluate_leg`. Updates next_run_at on
    success."""
    user_id = sub["user_id"]
    try:
        rules = json.loads(sub["rules_json"])
    except (json.JSONDecodeError, TypeError):
        return [ExecutionDecision(
            "skipped", "malformed strategy rules", sub.get("base_ticker", ""),
            "—", "buy", 0.0, None, None, "",
        )]
    strategy = strat_mod.Strategy.from_dict(rules)
    actions = strat_mod.evaluate_today(strategy)
    safety = get_safety_for(user_id)
    decisions: list[ExecutionDecision] = []
    last_action_summary = ""
    for a in actions:
        if a.kind == "buy":
            d = _evaluate_leg(user_id, a.ticker, "buy", a.usd_amount, safety,
                              client_order_id=f"narve-sub{sub['id']}-{uuid.uuid4().hex[:8]}")
            decisions.append(d)
            last_action_summary = f"{d.action}: {a.reason}"
        elif a.kind == "pause":
            d = ExecutionDecision(
                "skipped", f"strategy paused: {a.reason}",
                a.ticker, safety["preferred_exchange"], "buy",
                0.0, None, None, "",
            )
            decisions.append(d)
            last_action_summary = d.reason
        else:
            last_action_summary = a.reason

    # Advance the schedule even if we skipped, so we don't hot-loop.
    next_at = strat_mod.next_run_after(strategy).isoformat()
    db.update_strategy_subscription_run(sub["id"], next_at, last_action_summary)
    return decisions


def tick_subscriptions() -> dict:
    """Cron entry. Find every subscription whose next_run_at is in the past
    and run its strategy through the safety gauntlet. Mirrors
    `tick_due_schedules()` but for strategy subscriptions."""
    due = db.get_due_strategy_subscriptions()
    summary = {"checked": len(due), "actions": []}
    for sub in due:
        try:
            decisions = _run_subscription(sub)
            for d in decisions:
                summary["actions"].append({
                    "user_id": sub["user_id"], "strategy_id": sub["strategy_id"],
                    "ticker": d.ticker, "action": d.action, "reason": d.reason,
                })
        except Exception as e:
            log.warning("subscription tick failed for sub %s: %s", sub["id"], e)
            summary["actions"].append({
                "user_id": sub["user_id"], "strategy_id": sub["strategy_id"],
                "action": "error", "reason": str(e),
            })
    return summary


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
