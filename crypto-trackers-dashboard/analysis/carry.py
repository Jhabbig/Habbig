"""Funding-rate carry calculator.

Given a coin's median 8h funding rate, the annualised carry from
running a delta-neutral cash-and-carry trade (long spot, short perp) is:

  annualised_carry_pct = -median_funding_rate × 3 × 365 × 100

Positive funding = longs pay shorts, so a delta-neutral SHORT-perp
position EARNS that rate. We emit the carry per coin with the sign
flipped so positive carry means "you make money by shorting the perp +
holding spot." Negative carry means the opposite trade (long perp,
short spot) is the earner.

Pairs with analysis/funding.py which already joins Binance + Bybit.
"""
from __future__ import annotations

from typing import Optional


def annualised_carry(median_funding_rate: Optional[float]) -> Optional[float]:
    """Convert per-8h funding rate to annualised carry pct (sign flipped for
    cash-and-carry interpretation)."""
    if median_funding_rate is None:
        return None
    return round(-median_funding_rate * 3 * 365 * 100, 2)


def enrich_funding_rows(funding_rows: list[dict]) -> list[dict]:
    """Add carry_pct + carry_direction to each row."""
    out = []
    for r in funding_rows or []:
        carry = annualised_carry(r.get("median_rate"))
        direction = None
        if carry is not None:
            if carry > 0:
                direction = "SHORT_PERP_HOLD_SPOT"
            elif carry < 0:
                direction = "LONG_PERP_SHORT_SPOT"
            else:
                direction = "FLAT"
        out.append({**r, "carry_pct_annualised": carry,
                    "carry_direction": direction})
    return out
