"""Conditional forecasts: "if race X is won by D, how do the others shift?"

We use a **common-factor swing model**: every race's logit-probability moves
as a function of a single national-swing variable plus a chamber- and
region-specific component. Conditioning on the outcome of one race tells us
about that latent swing, and the update propagates to every other race in
proportion to its sensitivity.

Why this model rather than a full joint distribution: with 50+ races a
fully-specified covariance matrix is both intractable to estimate from the
data we have and impossible to display. The common-factor model captures
the dominant signal (wave elections favour one party across the board)
without overfitting.

Sensitivity (β) ranges:

  * **Senate / governor races** in swing states get β ≈ 1.0 (most reactive)
  * **House races in same-region states** get β scaled by region overlap
  * **Solidly partisan races** (forecast already near 0 or 1) get small β
    because they don't actually move much during a wave

We deliberately keep the swing UPDATE bounded so a single called race
doesn't push every other race to the same lean — that would be unrealistic.

Public functions:

  * ``compute_conditional`` — given a list of forecast rows and a single
    conditioned race ``(race_key, "D" | "R")``, return updated forecasts
  * ``correlation`` — pairwise race correlation used for shrinkage display
"""

from __future__ import annotations

import logging
import math
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Region buckets for cross-race correlation. Loosely the political-science
# "regional clusters" — races within the same bucket co-move more than races
# across buckets.
US_REGIONS: dict[str, str] = {
    # Northeast
    "ME": "NE", "NH": "NE", "VT": "NE", "MA": "NE", "RI": "NE", "CT": "NE",
    "NY": "NE", "NJ": "NE", "PA": "NE",
    # Midwest
    "OH": "MW", "MI": "MW", "IN": "MW", "IL": "MW", "WI": "MW", "MN": "MW",
    "IA": "MW", "MO": "MW", "KS": "MW", "NE": "MW", "ND": "MW", "SD": "MW",
    # South
    "VA": "S", "WV": "S", "KY": "S", "TN": "S", "NC": "S", "SC": "S",
    "GA": "S", "FL": "S", "AL": "S", "MS": "S", "LA": "S", "AR": "S",
    "TX": "S", "OK": "S", "DE": "S", "MD": "S", "DC": "S",
    # West / Mountain
    "MT": "W", "ID": "W", "WY": "W", "CO": "W", "UT": "W", "NV": "W",
    "AZ": "W", "NM": "W", "CA": "W", "OR": "W", "WA": "W", "AK": "W",
    "HI": "W",
}

# How much of the conditioned race's surprise propagates as a national-wave
# update. With this calibration: a coin-flip race resolving D shifts every
# competitive race by up to ~4pp toward D — modest but visible.
GLOBAL_SWING_SCALE = 0.20

# Max single-race update in probability points (caps so highly-correlated
# pairs don't slam into 0/1 unrealistically).
MAX_DELTA = 0.20


def _logit(p: float) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x > 30:
        return 1.0
    if x < -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _competitive_sensitivity(p: float) -> float:
    """Sensitivity peaks at p=0.5 (coin-flip races move the most) and drops
    to ~0 at the tails (a 95% D race barely budges on a normal wave)."""
    # 4*p*(1-p) is the variance of a Bernoulli normalized to peak=1 at 0.5
    return 4.0 * p * (1.0 - p)


def _region_factor(state_a: Optional[str], state_b: Optional[str]) -> float:
    if not state_a or not state_b:
        return 0.7
    if state_a == state_b:
        return 1.0
    ra = US_REGIONS.get(state_a.upper())
    rb = US_REGIONS.get(state_b.upper())
    if ra and rb and ra == rb:
        return 0.85
    return 0.65


def _chamber_factor(rt_a: Optional[str], rt_b: Optional[str]) -> float:
    if not rt_a or not rt_b:
        return 0.7
    if rt_a == rt_b:
        return 1.0
    # Senate / governor are statewide, share electorate → mid correlation.
    statewide = {"senate", "governor", "presidential"}
    if rt_a in statewide and rt_b in statewide:
        return 0.85
    if (rt_a in statewide) != (rt_b in statewide):
        return 0.55
    return 0.7


def correlation(race_a: dict, race_b: dict) -> float:
    """Pairwise race correlation in [0, 1] used for the propagation step.

    Inputs are forecast rows with at least ``state`` and ``race_type``.
    Diagonal is undefined — caller skips when ``a.race_key == b.race_key``.
    """
    region = _region_factor(race_a.get("state"), race_b.get("state"))
    chamber = _chamber_factor(race_a.get("race_type"), race_b.get("race_type"))
    return round(region * chamber, 4)


def compute_conditional(
    *,
    forecasts: list[dict],
    conditioned_race_key: str,
    conditioned_outcome: str,
) -> dict:
    """Re-score every race conditional on one race resolving for D or R.

    Args:
      forecasts: List of forecast rows (must include ``race_key``,
        ``forecast_d``, ``state``, ``race_type``). Other fields pass through.
      conditioned_race_key: The race we treat as resolved.
      conditioned_outcome: ``"D"`` or ``"R"``.

    Returns::

        {
          "conditioned": {"race_key": "...", "outcome": "D"},
          "swing_logit": 0.42,                # how much national logit shifted
          "races": [
            {"race_key": "...", "forecast_d": 0.55, "delta_pp": +3.2,
             "correlation": 0.62, ...},
            ...
          ],
        }
    """
    if conditioned_outcome not in ("D", "R"):
        raise ValueError(f"conditioned_outcome must be D or R, got {conditioned_outcome!r}")

    conditioned = next(
        (f for f in forecasts if f.get("race_key") == conditioned_race_key),
        None,
    )
    if conditioned is None or conditioned.get("forecast_d") is None:
        return {
            "conditioned": {"race_key": conditioned_race_key, "outcome": conditioned_outcome},
            "swing_logit": 0.0,
            "races": [{**f, "delta_pp": 0.0, "correlation": 0.0} for f in forecasts],
            "available": False,
        }

    prior_d = float(conditioned["forecast_d"])
    # Surprise = how unexpected the conditioned outcome was. A confident D win
    # carries no info; an upset is hugely informative.
    if conditioned_outcome == "D":
        surprise = (1.0 - prior_d)
    else:
        surprise = -prior_d

    # Convert surprise to a logit-space national-swing update. surprise=0
    # gives no update; surprise=±0.5 (mild upset) gives a meaningful shift.
    swing_logit = surprise * GLOBAL_SWING_SCALE * 4.0  # 4 makes the units feel right at peak

    out_races: list[dict] = []
    for f in forecasts:
        rk = f.get("race_key")
        p = f.get("forecast_d")
        if p is None:
            out_races.append({**f, "delta_pp": 0.0, "correlation": 0.0})
            continue
        if rk == conditioned_race_key:
            # Pin the conditioned race at the assumed outcome.
            new_p = 1.0 if conditioned_outcome == "D" else 0.0
            out_races.append({
                **f,
                "forecast_d": new_p,
                "delta_pp": round((new_p - float(p)) * 100, 2),
                "correlation": 1.0,
                "conditioned": True,
            })
            continue

        # Propagation: sensitivity × correlation × swing applied in logit space.
        corr = correlation(conditioned, f)
        sens = _competitive_sensitivity(float(p))
        delta_logit = swing_logit * corr * sens
        new_p = _sigmoid(_logit(float(p)) + delta_logit)
        delta = new_p - float(p)
        # Cap so a single race can't move another by an unrealistic amount.
        if abs(delta) > MAX_DELTA:
            delta = MAX_DELTA if delta > 0 else -MAX_DELTA
            new_p = float(p) + delta
            new_p = max(0.0, min(1.0, new_p))

        out_races.append({
            **f,
            "forecast_d": round(new_p, 4),
            "delta_pp": round(delta * 100, 2),
            "correlation": corr,
        })

    return {
        "conditioned": {"race_key": conditioned_race_key, "outcome": conditioned_outcome},
        "swing_logit": round(swing_logit, 4),
        "races": out_races,
        "available": True,
    }


def joint_distribution_summary(
    forecasts: list[dict],
    *,
    chamber: str,
) -> dict:
    """Approximate chamber-level seat distribution under the common-factor model.

    Rough Monte Carlo: draw the national swing from N(0, 1), reprice every
    competitive race, count expected D / R wins. This gives a smoother
    "expected seats" number than just counting forecast_d >= 0.5.

    Returns ``{"expected_d": float, "expected_r": float, "n_samples": int}``.
    """
    chamber_races = [
        f for f in forecasts
        if (f.get("race_type") or "").lower() == chamber.lower()
        and f.get("forecast_d") is not None
    ]
    if not chamber_races:
        return {"expected_d": 0.0, "expected_r": 0.0, "n_samples": 0}

    N = 1500
    # Deterministic-ish samples — quantiles of a standard normal — so the
    # endpoint is reproducible without numpy or a PRNG. 1500 evenly-spaced
    # quantiles give a smooth empirical distribution.
    samples: list[float] = []
    for i in range(N):
        # Convert a uniform quantile to a normal via the Beasley-Springer
        # approximation — simple, no scipy dependency.
        u = (i + 0.5) / N
        samples.append(_inv_normal_cdf(u))

    total_d = 0.0
    for s in samples:
        delta_logit = s * GLOBAL_SWING_SCALE
        for f in chamber_races:
            p = float(f["forecast_d"])
            sens = _competitive_sensitivity(p)
            new_p = _sigmoid(_logit(p) + delta_logit * sens)
            total_d += new_p
    expected_d = total_d / N
    return {
        "expected_d": round(expected_d, 2),
        "expected_r": round(len(chamber_races) - expected_d, 2),
        "n_samples": N,
        "chamber_total": len(chamber_races),
    }


def _inv_normal_cdf(p: float) -> float:
    """Acklam's approximation of the inverse standard-normal CDF. ~5 decimal
    places of accuracy; good enough for swing draws."""
    p = max(1e-9, min(1 - 1e-9, p))
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5]) * q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
