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
