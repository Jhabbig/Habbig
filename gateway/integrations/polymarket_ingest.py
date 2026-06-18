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
