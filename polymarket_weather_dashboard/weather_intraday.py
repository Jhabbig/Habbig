"""Intraday conditional-probability narrowing.

The dashboard already polls METAR every 5 minutes and tracks the
running daily high. The running max alone catches the trivial case
("11 am temp already exceeds threshold → P(YES)=1"), but throws away the
shape of the day's trajectory.

This module turns the trajectory into a sharper probability of the
final daily max:

  * If we've passed peak heating hours (typically 14:00–17:00 local) the
    remaining-day max can only come from a residual climb of a few
    degrees over the running max, sharply narrowing the prediction
    interval.
  * If we're pre-peak, the remaining hourly forecasts (already fetched
    by `fetch_hourly_at_station`) provide the upper end and we condition
    on what's already been observed below.
  * The bias-correction story carries: the *unobserved* portion of the
    day inherits the same per-station residual_std we compute elsewhere.

The math is deliberately simple — a normal distribution over the
remaining-day max with mean and sigma derived from the hourly forecast,
truncated below by the running max. That's all the leverage you need to
go from a 50% prior to 80–95% on a market that's already half resolved.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Optional

from scipy.stats import norm


# Typical peak-heating window (local time hour-of-day)
PEAK_HOUR_LOCAL_START = 13
PEAK_HOUR_LOCAL_END = 17


def _local_hour(timestamp_iso: str, utc_offset_hours: float = 0.0) -> Optional[int]:
    """Local hour-of-day from an ISO timestamp + UTC offset. Returns None
    on parse failure."""
    if not timestamp_iso:
        return None
    try:
        # Accept both Z-suffix and offset-aware ISO strings
        ts = timestamp_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
    except ValueError:
        try:
            dt = datetime.strptime(timestamp_iso[:19], "%Y-%m-%dT%H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Convert to local approximate hour
    local = dt.astimezone(timezone.utc)
    hour = (local.hour + utc_offset_hours) % 24
    return int(hour)


def remaining_hourly_max(hourly_temps: list[float], hours_elapsed: int) -> dict:
    """Pull the max of the remaining-day hourly forecasts.

    `hourly_temps` is a 24-length list (or shorter for partial days),
    `hours_elapsed` is how many entries we've already passed. Returns
    ``{max, mean, std}`` over the remainder, or empty when we're past
    the end of the day.
    """
    if not hourly_temps or hours_elapsed >= len(hourly_temps):
        return {"max": None, "mean": None, "std": None, "n": 0}
    remaining = [t for t in hourly_temps[hours_elapsed:] if t is not None]
    if not remaining:
        return {"max": None, "mean": None, "std": None, "n": 0}
    out = {
        "max": round(max(remaining), 2),
        "mean": round(sum(remaining) / len(remaining), 2),
        "n": len(remaining),
    }
    out["std"] = round(statistics.stdev(remaining), 2) if len(remaining) > 1 else 1.0
    return out


def conditional_max_distribution(running_max_f: Optional[float],
                                 hourly_temps: Optional[list[float]],
                                 hours_elapsed: int,
                                 station_residual_std: float = 2.5,
                                 ) -> Optional[dict]:
    """Estimate (mean, sigma) of the *final* daily max given the day so far.

    Parameters
    ----------
    running_max_f
        Highest observed °F so far. Acts as a hard lower bound on the
        final daily max.
    hourly_temps
        24-length list of forecast hourly temperatures for the day, in
        °F. May be partial; missing entries don't have to be ``None``.
    hours_elapsed
        How many hours of the day have already happened. Tells us where
        in `hourly_temps` we currently are.
    station_residual_std
        Empirical residual std for the unobserved portion. Falls back
        to 2.5 °F (typical 1-day MAE × √π/2).

    Returns
    -------
    dict
        ``{mean, std, hard_floor, n_remaining, source}`` or None if we
        don't have enough information to condition.
    """
    if running_max_f is None and not hourly_temps:
        return None

    # Past the end of the day: running_max IS the final max
    if hourly_temps is not None and hours_elapsed >= len(hourly_temps):
        if running_max_f is None:
            return None
        return {
            "mean": running_max_f,
            "std": 0.5,  # Tiny residual for measurement noise
            "hard_floor": running_max_f,
            "n_remaining": 0,
            "source": "post_peak_observed",
        }

    rem = remaining_hourly_max(hourly_temps or [], hours_elapsed)
    rem_max = rem.get("max")

    # Unobserved peak: the final max is max(running_max, future_peak).
    # We treat the future peak as N(rem_max, sigma_rem), where sigma_rem
    # comes from the station's empirical residual std and shrinks with
    # hours remaining (less time, less drift).
    if rem_max is not None:
        # Linearly shrink residual_std by remaining hours / 24
        n_rem = rem.get("n", 0) or 1
        sigma_rem = max(0.5, station_residual_std * math.sqrt(n_rem / 24.0))
        # Combine: final = max(running_max, future_peak)
        # Use the conservative E[max] approximation: max ≈ rem_max + 0.5*sigma
        # when running_max is below, else running_max.
        if running_max_f is not None and running_max_f >= rem_max + 0.5 * sigma_rem:
            return {
                "mean": running_max_f,
                "std": round(min(sigma_rem, 1.0), 2),
                "hard_floor": running_max_f,
                "n_remaining": n_rem,
                "source": "running_max_dominates",
            }
        # Predicted peak ahead — center on rem_max but raise mean if running
        # is in the same neighborhood.
        floor = running_max_f if running_max_f is not None else rem_max
        mean = max(rem_max, floor)
        return {
            "mean": round(mean, 2),
            "std": round(sigma_rem, 2),
            "hard_floor": floor,
            "n_remaining": n_rem,
            "source": "rem_hourly_peak",
        }

    # No hourly forecast — running max only. We have a one-sided prior:
    # the final max is at least running_max, with some chance of exceeding
    # by the station's typical afternoon climb. Use a one-sided shifted
    # normal.
    if running_max_f is not None:
        return {
            "mean": running_max_f + 0.5 * station_residual_std,
            "std": station_residual_std,
            "hard_floor": running_max_f,
            "n_remaining": 0,
            "source": "running_max_only",
        }
    return None


def conditional_probability_above(threshold_f: float,
                                  conditional_dist: dict) -> Optional[float]:
    """P(daily_max ≥ threshold) under the conditional distribution.

    Honors the hard floor: if running_max already meets the threshold,
    return 1.0 (the market has effectively resolved). The Gaussian
    component handles the "still possibly higher" mass when relevant.
    """
    if not conditional_dist:
        return None
    mean = conditional_dist.get("mean")
    std = conditional_dist.get("std")
    floor = conditional_dist.get("hard_floor")
    if mean is None or std is None or std <= 0:
        return None
    if floor is not None and floor >= threshold_f:
        return 0.99
    p = 1.0 - norm.cdf(threshold_f, loc=mean, scale=std)
    return max(0.01, min(0.99, float(p)))


def conditional_probability_below(threshold_f: float,
                                  conditional_dist: dict) -> Optional[float]:
    """P(daily_max ≤ threshold). Mirror of `conditional_probability_above`."""
    if not conditional_dist:
        return None
    mean = conditional_dist.get("mean")
    std = conditional_dist.get("std")
    floor = conditional_dist.get("hard_floor")
    if mean is None or std is None or std <= 0:
        return None
    # If running_max already exceeds threshold, "below" is impossible
    if floor is not None and floor > threshold_f:
        return 0.01
    p = norm.cdf(threshold_f, loc=mean, scale=std)
    return max(0.01, min(0.99, float(p)))


def conditional_probability(threshold_info: dict, conditional_dist: dict
                            ) -> Optional[float]:
    """Score a threshold_info dict against a conditional distribution.

    Mirrors the shape of `weather_calibration.gaussian_probability` but
    operates on the post-conditioning distribution rather than the prior
    forecast. Celsius thresholds are converted to °F first.
    """
    if not conditional_dist or not threshold_info:
        return None
    is_celsius = (threshold_info.get("unit", "F") or "F").upper().startswith("C")
    threshold = threshold_info.get("threshold")
    is_over = threshold_info.get("is_over")
    lower = threshold_info.get("temp_lower")
    upper = threshold_info.get("temp_upper")
    if is_celsius:
        from weather_pure import c_to_f
        if threshold is not None:
            threshold = c_to_f(threshold)
        if lower is not None:
            lower = c_to_f(lower)
        if upper is not None:
            upper = c_to_f(upper)

    if threshold is not None and is_over is not None:
        if is_over:
            return conditional_probability_above(threshold, conditional_dist)
        return conditional_probability_below(threshold, conditional_dist)
    if lower is not None and upper is not None:
        # Range — use the unconditional distribution between the two endpoints
        mean = conditional_dist["mean"]
        std = conditional_dist["std"]
        floor = conditional_dist.get("hard_floor")
        # If running_max exceeded upper, the range is impossible
        if floor is not None and floor > upper:
            return 0.01
        # If running_max is inside the range, we know we're at least there;
        # range probability becomes P(no further climb past upper)
        if floor is not None and lower <= floor <= upper:
            p_stay = norm.cdf(upper + 0.5, loc=mean, scale=std)
            return max(0.01, min(0.99, float(p_stay)))
        p = norm.cdf(upper + 0.5, loc=mean, scale=std) - norm.cdf(lower - 0.5,
                                                                  loc=mean, scale=std)
        return max(0.01, min(0.99, float(p)))
    return None
