"""Math helpers used across prediction models."""
from __future__ import annotations

import math
from typing import Optional


def normal_cdf(x: float) -> float:
    """Standard normal cumulative distribution function via erf."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def linear_regression(xs: list[float], ys: list[float]) -> Optional[tuple[float, float, float]]:
    """Least-squares fit. Returns (slope, intercept, in_sample_residual_std).

    Callers typically apply a domain-specific floor to the residual std so that
    forecast bands don't collapse to zero on suspiciously clean data.
    """
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = num / den
    intercept = my - slope * mx
    resid = [ys[i] - (intercept + slope * xs[i]) for i in range(n)]
    sigma = math.sqrt(sum(r * r for r in resid) / n)
    return slope, intercept, sigma
