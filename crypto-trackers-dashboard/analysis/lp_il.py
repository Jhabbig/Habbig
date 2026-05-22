"""Liquidity-provider impermanent-loss calculator.

For a 50/50 constant-product AMM pool (Uniswap V2-style), if a token's
price changes by ratio r (= price_now / price_initial) while the
opposite asset (usually a stable) stays at $1, the impermanent loss
relative to just holding both halves is:

    IL_pct = 2 * sqrt(r) / (1 + r) - 1

So if a coin doubles (r=2), IL = 2 * sqrt(2) / 3 - 1 = -5.7% — you have
~5.7% less value than if you'd just held both assets equally.

For concentrated-liquidity pools (Uniswap V3) the math is messier — the
LP is more leveraged within their chosen range, so IL within range is
greater than V2. We expose both V2 and a V3 approximation.

Pure math, no upstream data. Tests its own boundary cases.
"""
from __future__ import annotations

import math
from typing import Optional


def il_pct_v2(price_ratio: float) -> Optional[float]:
    """Impermanent loss for a V2-style 50/50 pool.

    ``price_ratio`` = current_price / initial_price.  Returns the IL as
    a percentage (negative number; -5.7% if a coin doubles).
    """
    if price_ratio is None or price_ratio <= 0:
        return None
    r = float(price_ratio)
    pool_value_ratio = 2 * math.sqrt(r) / (1 + r)
    return (pool_value_ratio - 1) * 100


def il_pct_v3(price_ratio: float, range_low: float, range_high: float) -> Optional[float]:
    """Concentrated-liquidity IL with a range [low, high] relative to initial.

    A V3 LP behaves like a V2 LP times a leverage factor that depends on
    how tight the range is. Approximation:

        leverage = sqrt(p_high) / (sqrt(p_high) - sqrt(p_low)) for the
        in-range case.

    Out-of-range the LP becomes 100% of one asset and IL = (price_ratio - 1)*100
    or 0 depending on side.
    """
    if (price_ratio is None or price_ratio <= 0
        or range_low is None or range_high is None
        or range_low <= 0 or range_high <= range_low):
        return None
    if price_ratio < range_low:
        # Position becomes 100% of the deposited token (token0). IL is the loss
        # vs holding 50/50 - but with the price down, the LP holds *more* of
        # the depreciated token, so the loss matches the price drop.
        return (price_ratio - 1) * 100
    if price_ratio > range_high:
        # Out the top - position is 100% of token1 (stable). LP missed the upside.
        return -((price_ratio - 1) / price_ratio) * 100
    # In-range — pretty close to V2 amplified
    leverage = math.sqrt(range_high) / (math.sqrt(range_high) - math.sqrt(range_low))
    base = il_pct_v2(price_ratio) or 0
    return base * leverage


def il_grid(price_ratios: list[float] | None = None) -> list[dict]:
    """A grid of common scenarios for the calculator widget."""
    ratios = price_ratios or [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0, 10.0]
    return [{
        "price_ratio": r,
        "price_change_pct": (r - 1) * 100,
        "il_v2_pct": round(il_pct_v2(r) or 0, 3),
    } for r in ratios]


if __name__ == "__main__":
    import json
    print(json.dumps(il_grid(), indent=2))
    print("V3 [0.5, 2.0] with r=1.5:", il_pct_v3(1.5, 0.5, 2.0))
    print("V3 [0.5, 2.0] with r=2.5 (out of range):", il_pct_v3(2.5, 0.5, 2.0))
