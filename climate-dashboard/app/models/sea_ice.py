"""Sea ice models — daily extent record-rank check + per-hemisphere annual-min projection.

For each hemisphere we take the per-year minimum extent across the daily
record, then linear-regress the last 25 years of those minima against year.
The current year is excluded from the fit until its minimum is reached
(mid-Sep Arctic, mid-Mar Antarctic) so we project FOR it instead of with it.
"""
from __future__ import annotations

from typing import Optional

from ..math_utils import linear_regression

_MIN_FIT_YEARS = 5
_FIT_WINDOW_YEARS = 25
_ARCTIC_MIN_SIGMA = 0.1
_ANTARCTIC_MIN_SIGMA = 0.15


def daily_record_check(sea_ice: dict) -> Optional[dict]:
    """Today's Arctic extent vs the historical min/max for this day-of-year."""
    if not sea_ice or not sea_ice.get("arctic"):
        return None
    series = sea_ice["arctic"]
    if not series:
        return None
    latest = series[-1]
    doy_lat = (latest["month"], latest["day"])
    same_doy = [s for s in series
                if (s["month"], s["day"]) == doy_lat and s["year"] != latest["year"]]
    if not same_doy:
        return None
    extents = [s["extent_mkm2"] for s in same_doy]
    rank = 1 + sum(1 for e in extents if e < latest["extent_mkm2"])
    return {
        "date": f"{latest['year']:04d}-{latest['month']:02d}-{latest['day']:02d}",
        "extent_mkm2": latest["extent_mkm2"],
        "doy_min": round(min(extents), 4),
        "doy_max": round(max(extents), 4),
        "doy_mean": round(sum(extents) / len(extents), 4),
        "rank_lowest_in_record": rank,
        "history_years": len(extents) + 1,
    }


def _is_post_arctic_min(month: int, day: int) -> bool:
    # Arctic minimum is typically mid-September. After ~Sep 15 we treat the
    # year's minimum as found. Months 10-12 + early-January count as "post-min"
    # too — only Jan-Aug + early-Sep are pre-min.
    return month > 9 or (month == 9 and day >= 15)


def _is_post_antarctic_min(month: int, day: int) -> bool:
    # Antarctic min is typically Feb 14 – early March. After ~Mar 15 we treat
    # the year's minimum as found.
    return month > 3 or (month == 3 and day >= 15)


def _annual_min_projection(series: list[dict],
                           is_post_min_fn,
                           min_sigma: float) -> Optional[dict]:
    if not series:
        return None
    by_year: dict[int, float] = {}
    by_year_doy: dict[int, tuple[int, int]] = {}
    for s in series:
        y = s["year"]
        e = s["extent_mkm2"]
        if y not in by_year or e < by_year[y]:
            by_year[y] = e
            by_year_doy[y] = (s["month"], s["day"])
    if len(by_year) < 10:
        return None
    cur_year = max(by_year.keys())
    cur_doy = by_year_doy[cur_year]
    is_post_min = is_post_min_fn(*cur_doy)
    fit_years = sorted(y for y in by_year if y != cur_year or is_post_min)
    fit_years = [y for y in fit_years if y >= cur_year - _FIT_WINDOW_YEARS]
    if len(fit_years) < _MIN_FIT_YEARS:
        return None
    fit = linear_regression([float(y) for y in fit_years], [by_year[y] for y in fit_years])
    if fit is None:
        return None
    slope, intercept, sigma = fit
    proj = intercept + slope * cur_year
    return {
        "current_year": cur_year,
        "fit_window_years": len(fit_years),
        "trend_mkm2_per_year": round(slope, 4),
        "projected_min_mkm2": round(proj, 3),
        "residual_std_mkm2": round(max(sigma, min_sigma), 3),
        "is_post_min": is_post_min,
    }


def arctic_min_projection(sea_ice: dict) -> Optional[dict]:
    if not sea_ice or not sea_ice.get("arctic"):
        return None
    return _annual_min_projection(sea_ice["arctic"], _is_post_arctic_min, _ARCTIC_MIN_SIGMA)


def antarctic_min_projection(sea_ice: dict) -> Optional[dict]:
    if not sea_ice or not sea_ice.get("antarctic"):
        return None
    return _annual_min_projection(sea_ice["antarctic"], _is_post_antarctic_min, _ANTARCTIC_MIN_SIGMA)


def annual_extremes(series: list[dict]) -> list[dict]:
    """Per-year min and max extent. Used by the overlay chart so the frontend
    doesn't need the full daily history. Returns a list sorted by year."""
    if not series:
        return []
    lo: dict[int, float] = {}
    hi: dict[int, float] = {}
    for s in series:
        y = s["year"]
        e = s["extent_mkm2"]
        if y not in lo or e < lo[y]:
            lo[y] = e
        if y not in hi or e > hi[y]:
            hi[y] = e
    return [
        {"year": y, "min_mkm2": round(lo[y], 4), "max_mkm2": round(hi[y], 4)}
        for y in sorted(lo)
    ]
