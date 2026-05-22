"""Position-size + risk calculator.

Given account size, risk %, entry, stop, and (optionally) target,
returns:
  - position_size_usd: max USD to deploy so that a stop-out loses
    exactly risk_pct of the account
  - position_size_tokens: that USD ÷ entry
  - risk_per_unit_usd: |entry - stop|
  - max_loss_usd: position_size_tokens × risk_per_unit_usd
  - reward_per_unit_usd: |target - entry|  (when target provided)
  - rr_ratio: reward / risk
  - max_gain_usd: position_size_tokens × reward_per_unit_usd
  - rr_break_even_pct: |stop - entry| / entry × 100

Pure math; no upstream calls.
"""
from __future__ import annotations

from typing import Optional


def size(
    *, account_usd: float, risk_pct: float,
    entry: float, stop: float, target: Optional[float] = None,
    leverage: float = 1.0, fee_pct: float = 0.1,
) -> dict:
    if account_usd <= 0 or risk_pct <= 0 or entry <= 0 or stop <= 0:
        return {"error": "account / risk / entry / stop must be positive"}
    if entry == stop:
        return {"error": "entry and stop cannot be equal"}
    risk_per_unit = abs(entry - stop)
    risk_usd = account_usd * (risk_pct / 100)
    # Account for round-trip fees: reduce risk budget by expected fee cost
    fee_per_unit = entry * (fee_pct / 100) * 2  # entry + exit
    effective_risk_per_unit = risk_per_unit + fee_per_unit
    tokens = risk_usd / effective_risk_per_unit
    position_usd = tokens * entry
    max_loss_usd = risk_usd  # by construction
    side = "LONG" if entry > stop else "SHORT"
    out: dict = {
        "side": side,
        "account_usd": account_usd,
        "risk_pct": risk_pct,
        "max_loss_usd": round(max_loss_usd, 2),
        "position_size_tokens": round(tokens, 8),
        "position_size_usd": round(position_usd, 2),
        "position_pct_of_account": round((position_usd / account_usd) * 100, 2),
        "risk_per_unit_usd": round(risk_per_unit, 6),
        "stop_distance_pct": round(abs(entry - stop) / entry * 100, 3),
        "leverage": leverage,
        "leveraged_position_usd": round(position_usd * leverage, 2),
        "fee_drag_usd": round(tokens * fee_per_unit, 2),
    }
    if target and target > 0:
        reward_per_unit = abs(target - entry)
        rr = reward_per_unit / risk_per_unit if risk_per_unit else 0
        max_gain = tokens * reward_per_unit
        out.update({
            "target": target,
            "reward_per_unit_usd": round(reward_per_unit, 6),
            "rr_ratio": round(rr, 3),
            "max_gain_usd": round(max_gain, 2),
            "target_distance_pct": round(abs(target - entry) / entry * 100, 3),
            "expected_value_at_50pct_winrate_usd": round(0.5 * max_gain - 0.5 * max_loss_usd, 2),
        })
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(size(account_usd=10_000, risk_pct=1,
                          entry=70_000, stop=68_000, target=78_000), indent=2))
