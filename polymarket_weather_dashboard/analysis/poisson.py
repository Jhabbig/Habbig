"""Poisson tail probabilities used by the disaster models.

Two helpers:

  * ``p_at_least(lam, k)`` - P(N >= k) where N ~ Poisson(lam).
  * ``p_between(lam, lo, hi)`` - P(lo <= N <= hi).

Implemented as small loops (no scipy dependency) since all the lambdas we
deal with are well below 1000, where the direct sum is stable and fast.
"""
from __future__ import annotations

import math
from typing import Optional


def p_at_least(lam: float, k: int) -> Optional[float]:
    if lam is None or lam < 0:
        return None
    if k <= 0:
        return 1.0
    total = 0.0
    fact = 1.0
    pwr = 1.0
    for kk in range(k):
        if kk > 0:
            fact *= kk
            pwr *= lam
        total += math.exp(-lam) * pwr / fact
    return max(0.0, min(1.0, 1.0 - total))


def p_between(lam: float, lo: int, hi: int) -> Optional[float]:
    if lam is None or lam < 0:
        return None
    if hi < lo:
        return 0.0
    p_lo = p_at_least(lam, lo)
    p_hi_plus_1 = p_at_least(lam, hi + 1)
    if p_lo is None or p_hi_plus_1 is None:
        return None
    return max(0.0, min(1.0, p_lo - p_hi_plus_1))
