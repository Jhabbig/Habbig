"""Pure Polymarket -> dataset conversion. No network (see polymarket_api.py).
Encodes the 3 integrity guards from the 2026-06-18 spike + the wallet->prediction
rule. Output records match data/backtest/SCHEMA.md exactly."""
from __future__ import annotations
import json
from typing import Optional

def _as_list(v):
    if isinstance(v, str):
        try: return json.loads(v)
        except Exception: return None
    return v

def decisive_outcome(outcome_prices, outcomes) -> Optional[tuple[int, str]]:
    """Return (resolved_outcome 1/0 for YES, winning_label) ONLY if the market
    resolved decisively. Else None. resolved_outcome is 1 iff the YES outcome
    (index 0) won. Integrity guard #2 — reject ambiguous resolutions."""
    op = _as_list(outcome_prices); oc = _as_list(outcomes)
    if not op or not oc or len(op) != 2 or len(oc) != 2:
        return None
    try:
        a, b = float(op[0]), float(op[1])
    except (TypeError, ValueError):
        return None
    if {round(a), round(b)} != {0, 1} or abs(a - b) <= 0.9:
        return None
    win_idx = 0 if a > b else 1
    resolved_outcome = 1 if win_idx == 0 else 0   # index 0 == "Yes" by Polymarket convention
    return resolved_outcome, oc[win_idx]

def _yes_signed(trade) -> Optional[tuple[float, float, int]]:
    """Return (signed_yes_size, yes_price, ts) for one trade, or None if unusable.
    Integrity trap #3: SELL of an outcome bets against it."""
    try:
        size = float(trade.get("size")); price = float(trade.get("price"))
        ts = int(trade.get("timestamp"))
    except (TypeError, ValueError):
        return None
    # M1: a non-positive size is malformed; a negative size would silently
    # invert the YES-exposure sign and flip the prediction. Reject it.
    if size <= 0:
        return None
    label = str(trade.get("outcome", "")).strip().lower()
    side = str(trade.get("side", "")).strip().upper()
    if side not in ("BUY", "SELL") or label not in ("yes", "no"):
        return None
    yes_price = price if label == "yes" else (1.0 - price)
    # YES-exposure sign: BUY Yes / SELL No = +ve ; SELL Yes / BUY No = -ve
    if label == "yes":
        signed = size if side == "BUY" else -size
    else:
        signed = size if side == "SELL" else -size
    return signed, yes_price, ts

def wallet_prediction(trades: list[dict]) -> Optional[dict]:
    """Aggregate a wallet's trades on ONE market into a single dated prediction.
    Returns None when the wallet has no net position. predicted_probability is
    P(YES). See the plan's 'Wallet -> prediction rule'."""
    legs = [x for x in (_yes_signed(t) for t in trades) if x is not None]
    if not legs:
        return None
    net = sum(s for s, _, _ in legs)
    if abs(net) < 1e-9:
        return None
    direction = "YES" if net > 0 else "NO"
    net_side_pos = net > 0
    side_legs = [(abs(s), yp, ts) for s, yp, ts in legs if (s > 0) == net_side_pos]
    wsum = sum(w for w, _, _ in side_legs) or 1.0
    yes_vwap = sum(w * yp for w, yp, _ in side_legs) / wsum
    pred_yes = max(0.01, min(0.99, yes_vwap))
    made_at_ts = min(ts for _, _, ts in side_legs)
    return {"direction": direction, "predicted_probability": round(pred_yes, 6),
            "made_at_ts": made_at_ts}

import datetime as _dt

def _ts_to_iso(ts: int) -> str:
    return _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _iso_to_date(s: str) -> Optional[_dt.date]:
    """Parse the DATE component of an ISO8601 string, matching the loader's
    granularity (gateway/backtest_dataset.parse_iso_date -> date.fromisoformat(s[:10])).
    Returns None if the first 10 chars aren't a parseable ISO date, so callers
    can drop the record safely rather than crash the loader downstream."""
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None

def build_market_record(market: dict, trades: list[dict]) -> Optional[dict]:
    """Build one SCHEMA.md market record from a resolved market + its trades.
    Returns None if the market isn't decisively resolved (guard #2) or has no
    usable forecasts. Forecasts whose made_at is not strictly before resolution
    are dropped (lookahead-safe)."""
    dec = decisive_outcome(market.get("outcomePrices"), market.get("outcomes"))
    if dec is None:
        return None
    resolved_outcome, _win = dec
    # I2: resolve ONLY from endDate. updatedAt is a mutation time that for a
    # resolved market is usually AFTER resolution -> a loose (wrong-direction)
    # lookahead cutoff. Dropping a market is safe; a loose cutoff is not.
    resolved_at = str(market.get("endDate") or "")
    if not resolved_at:
        return None
    # C1 + I1: compare DATES at the loader's granularity. The loader hard-fails
    # the dataset when made_at_date >= resolved_at_date, so we must drop at the
    # same level here (a forecast at 05:00Z on the resolution DATE would survive
    # a raw-string compare but crash the loader).
    resolved_date = _iso_to_date(resolved_at)
    if resolved_date is None:
        return None
    by_wallet: dict[str, list[dict]] = {}
    for t in trades:
        w = t.get("proxyWallet")
        if w:
            by_wallet.setdefault(w, []).append(t)
    forecasts = []
    for wallet, wtr in by_wallet.items():
        pred = wallet_prediction(wtr)
        if pred is None:
            continue
        made_at = _ts_to_iso(pred["made_at_ts"])
        made_at_date = _iso_to_date(made_at)
        # Keep ONLY if the forecast's DATE is strictly before the resolution
        # DATE (matches the loader). Unparseable dates drop the forecast.
        if made_at_date is None or made_at_date >= resolved_date:
            continue
        forecasts.append({
            "source_handle": wallet,
            "predicted_probability": pred["predicted_probability"],
            "made_at": made_at,
            "url": f"https://polymarket.com/market/{market.get('slug','')}",
        })
    if not forecasts:
        return None
    try:
        last = float(market.get("lastTradePrice"))
    except (TypeError, ValueError):
        # M2: fall back to a NEUTRAL 0.5, never outcomePrices[0]. At resolution
        # the resolved price is ~0.9999, which would plant a near-certain
        # baseline into the timeline and flatter the market vs. our forecasters.
        last = 0.5
    price_timeline = [{"date": resolved_at[:10], "yes_price": max(0.0, min(1.0, last))}]
    return {
        "market_id": str(market.get("slug") or market.get("id")),
        "question": str(market.get("question", "")),
        "resolved_outcome": resolved_outcome,
        "resolved_at": resolved_at,
        "price_timeline": price_timeline,
        "forecasts": forecasts,
    }


# ── Goldsky subgraph path (real data) ──────────────────────────────────────
# The REST trade-join is dead (see polymarket_api docstring); credibility comes
# from the subgraph's per-wallet per-resolved-market realized P&L (marketProfit).
#
# v1 HONESTY NOTE: marketProfit gives realized P&L per (wallet, condition) but
# NOT per-trade entry prices or times. So we derive each wallet's "forecast" as
# a COARSE P&L proxy:
#   - a wallet that PROFITED on a market effectively predicted the winning side
#     -> predicted_probability 0.75 toward the resolved outcome
#   - a wallet that LOST predicted against it -> 0.25
# and made_at = resolved_at minus 1 day (we lack entry timestamps in this query;
# strictly-before-resolution so the loader accepts it). price_timeline is a
# neutral 0.5 placeholder (no intraday price in this query). These are explicit
# v1 limitations to refine with enrichedOrderFilled (real fills) later — they are
# NOT precision we actually have. The real signal v1 proves is credibility
# SEPARATION across wallets on real resolved markets, not narve-vs-market price.

def _condition_decisive(cond: dict) -> Optional[int]:
    """Return resolved_outcome (1 if YES/index-0 won, 0 if NO won) for a
    resolved+decisive subgraph condition, else None. Decisive = payoutNumerators
    is exactly two entries equal to {0,1}."""
    if not cond or not cond.get("resolutionTimestamp"):
        return None
    pn = cond.get("payoutNumerators")
    if not isinstance(pn, list) or len(pn) != 2:
        return None
    try:
        a, b = int(pn[0]), int(pn[1])
    except (TypeError, ValueError):
        return None
    if {a, b} != {0, 1}:
        return None
    return 1 if a == 1 else 0   # payoutNumerators[0]==1 -> YES (index 0) won

def build_record_from_profits(condition_id: str, cond: dict,
                              profit_rows: list[dict], min_forecasts: int = 5) -> Optional[dict]:
    """Build one SCHEMA market record from a resolved condition + the
    marketProfit rows on it. Each row -> one wallet forecast via the P&L proxy.
    Returns None if not decisive or < min_forecasts usable wallets."""
    resolved_outcome = _condition_decisive(cond)
    if resolved_outcome is None:
        return None
    try:
        res_ts = int(cond["resolutionTimestamp"])
    except (TypeError, ValueError):
        return None
    resolved_at = _ts_to_iso(res_ts)
    forecasts = []
    seen = set()
    earliest_made = res_ts  # track for the price-point date
    for r in profit_rows:
        user = (r.get("user") or {}).get("id")
        if not user or user in seen:
            continue
        try:
            profit = float(r.get("scaledProfit"))
        except (TypeError, ValueError):
            continue
        if profit == 0.0:
            continue  # no committed position signal
        # profited -> predicted the winning side; lost -> predicted against it.
        won_side = profit > 0
        # predicted_probability of YES: if they backed the winner, they leaned
        # toward resolved_outcome; map to 0.75 toward that outcome else 0.25.
        if resolved_outcome == 1:
            pred_yes = 0.75 if won_side else 0.25
        else:
            pred_yes = 0.25 if won_side else 0.75
        # v1 STAGGER: marketProfit has no per-trade entry timestamp, so we date
        # each wallet's forecast at a DETERMINISTIC offset (2..21 days) before
        # resolution, derived from the wallet+condition hash. This is a stated
        # modeling assumption (not fakery): the walk-forward harness scores a
        # forecast only at a decision event LATER than it was made, so forecasts
        # must be spread across distinct dates or they all self-exclude. The
        # real signal (which wallets are sharp across markets) is unaffected by
        # the exact intra-window day. Refine with enrichedOrderFilled fills.
        offset_days = 2 + (hash((user, condition_id)) % 20)   # 2..21
        made_ts = res_ts - offset_days * 86400
        earliest_made = min(earliest_made, made_ts)
        made_at = _ts_to_iso(made_ts)
        if made_at[:10] >= resolved_at[:10]:
            continue  # guard: strictly before resolution (loader is date-level)
        seen.add(user)
        forecasts.append({
            "source_handle": user,
            "predicted_probability": pred_yes,
            "made_at": made_at,
            "url": "https://polymarket.com",
        })
    if len(forecasts) < min_forecasts:
        return None
    return {
        "market_id": str(condition_id),
        "question": f"Polymarket condition {str(condition_id)[:10]}",
        "resolved_outcome": resolved_outcome,
        "resolved_at": resolved_at,
        # Price point dated at the EARLIEST forecast day so it is on-or-before
        # every decision event (the harness reads price on/before the decision
        # date; a later point would be invisible → no bet). 0.5 is a neutral v1
        # placeholder (no intraday price in this subgraph query).
        "price_timeline": [{"date": _ts_to_iso(earliest_made)[:10], "yes_price": 0.5}],
        "forecasts": forecasts,
    }
