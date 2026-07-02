"""Negative-binomial helpers for overdispersed counts.

Disaster event counts (Atlantic named storms, US tornadoes, M6+ quakes,
wildfires) are systematically *overdispersed* relative to Poisson — the
year-to-year variance exceeds the mean. Using Poisson(lambda) understates
tail probability mass and produces over-confident edges.

The negative binomial distribution generalises Poisson with an extra
dispersion parameter. Parameterised as NB(mu, alpha) where:

    Var(X) = mu + alpha * mu^2

so alpha=0 recovers Poisson. Empirical alpha for each domain (sourced from
historical NOAA / SPC / USGS data, 1980-2024):

    domain                 mean    var      alpha
    Atlantic named storms  ~14     ~22      0.04
    US tornadoes           ~1249   ~62500   0.039
    Global M5+ quakes      ~1500   ~3000    0.0007
    Global M6+ quakes      ~140    ~250     0.006
    NIFC acres burned (M)   ~7      ~7       not modelled here (Normal works)

The dispersion captures regime variance (active vs quiet seasons) that a
single-rate Poisson can't see.

We compute P(X >= k) under NB(mu, alpha) using the negative-binomial CDF
formulation in terms of the regularised incomplete beta function. To keep
us scipy-free we use a numeric continued-fraction expansion of the
incomplete beta - same approach textbooks use for chi-squared p-values.
"""
from __future__ import annotations

import math
from typing import Optional


# Empirical dispersion (alpha) values per domain - tuned against
# 1980-2024 NOAA/SPC/USGS year-end series.
ALPHA = {
    "atlantic_named_storms": 0.040,
    "atlantic_hurricanes":   0.060,  # slightly more dispersed than named
    "atlantic_major_hurricanes": 0.090,
    "us_tornadoes":          0.039,
    "global_m5":             0.0007,
    "global_m6":             0.006,
    "global_m7":             0.020,
    "fema_dr":               0.020,
    "wildfire_count":        0.030,
}


def _log_betacf(a: float, b: float, x: float, max_iter: int = 200, eps: float = 1e-12) -> float:
    """Continued-fraction expansion for the incomplete beta function.

    Lentz's algorithm. Returns ln(beta_cf(a, b, x)).
    """
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m_ in range(1, max_iter + 1):
        m2 = 2 * m_
        aa = m_ * (b - m_) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m_) * (qab + m_) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return math.log(h)


def regularised_incomplete_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b) - regularised incomplete beta. Uses series + continued fraction."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    # log(B(a,b)) via lgamma
    log_b = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    bt = math.exp(a * math.log(x) + b * math.log(1.0 - x) - log_b)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * math.exp(_log_betacf(a, b, x)) / a
    return 1.0 - bt * math.exp(_log_betacf(b, a, 1.0 - x)) / b


def nb_cdf_at_least(k: int, mu: float, alpha: float) -> float:
    """P(X >= k) where X ~ NB(mean=mu, dispersion=alpha).

    The NB(mu, alpha) parameterisation has variance mu + alpha*mu^2.
    Internally we convert to the (r, p) parameterisation:

        r = 1/alpha
        p = r / (r + mu)

    Then P(X < k) = I_p(r, k) where I is the regularised incomplete beta.

    Fallback: when alpha is 0 we degenerate to Poisson via the standard
    series (no need for the beta detour).
    """
    if k <= 0:
        return 1.0
    if mu <= 0:
        return 0.0
    if alpha <= 1e-9:
        # Poisson tail via direct series
        total = 0.0
        fact = 1.0
        pwr = 1.0
        for kk in range(k):
            if kk > 0:
                fact *= kk
                pwr *= mu
            total += math.exp(-mu) * pwr / fact
        return max(0.0, min(1.0, 1.0 - total))
    r = 1.0 / alpha
    p = r / (r + mu)
    cdf_below = regularised_incomplete_beta(r, float(k), p)
    return max(0.0, min(1.0, 1.0 - cdf_below))


def nb_between(lo: int, hi: int, mu: float, alpha: float) -> float:
    if hi < lo:
        return 0.0
    p_lo = nb_cdf_at_least(lo, mu, alpha)
    p_hi_plus_1 = nb_cdf_at_least(hi + 1, mu, alpha)
    return max(0.0, min(1.0, p_lo - p_hi_plus_1))


def nb_quantile_band(mu: float, alpha: float, *, ci: float = 0.80) -> Optional[tuple[int, int]]:
    """Approximate a (lower, upper) credible interval for X ~ NB(mu, alpha).

    For ci=0.80 we return the [10%, 90%] quantiles - useful for the UI's
    "we expect 12-19 storms this year" framing.

    Implementation uses bisection over P(X >= k); since we don't have a
    closed-form NB quantile we walk integer k from 0 upward until the tail
    crosses the CI bound. With mu well below 10000 this is fast.
    """
    if mu <= 0:
        return None
    lower_p = (1.0 - ci) / 2.0
    upper_p = 1.0 - lower_p
    # search k in [0, ceil(mu * 10)] - comfortably wide
    high_k = max(int(mu * 10) + 20, int(mu + 6 * math.sqrt(max(mu, 1.0))) + 5)
    lower_k: Optional[int] = None
    upper_k: Optional[int] = None
    for k in range(high_k + 1):
        p_at_least = nb_cdf_at_least(k, mu, alpha)
        if lower_k is None and p_at_least <= upper_p:
            lower_k = max(0, k - 1)
        if p_at_least <= 1.0 - upper_p:
            upper_k = k
            break
    if lower_k is None:
        lower_k = 0
    if upper_k is None:
        upper_k = high_k
    return lower_k, upper_k


if __name__ == "__main__":
    # Sanity: NB(mu=14, alpha=0.04) should have wider tails than Poisson(14).
    import json
    print("Atlantic named storms - NB(mu=14, alpha=0.04):")
    print("  P(>=14):", round(nb_cdf_at_least(14, 14, 0.04), 4))
    print("  P(>=20):", round(nb_cdf_at_least(20, 14, 0.04), 4))
    print("  P(>=25):", round(nb_cdf_at_least(25, 14, 0.04), 4))
    print("  Poisson(14) P(>=20):",
          round(nb_cdf_at_least(20, 14, 0.0), 4),
          "<- tighter (overconfident in tail)")
    band80 = nb_quantile_band(14, 0.04, ci=0.80)
    band95 = nb_quantile_band(14, 0.04, ci=0.95)
    print("  80% CI:", band80)
    print("  95% CI:", band95)
    # Sanity: more-dispersed -> wider band
    band_strong = nb_quantile_band(14, 0.20, ci=0.80)
    print("  alpha=0.20 -> 80% CI:", band_strong, "(should be wider)")
