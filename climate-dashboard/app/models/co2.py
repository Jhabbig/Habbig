"""Year-end CO₂ projection.

24-month linear regression on Mauna Loa monthly means, evaluated at the
decimal date of the next year's start (≈ year-end). Forecast bands use the
in-sample residual std with a 0.3 ppm floor — Mauna Loa is too clean to
plausibly claim less than that as forecast uncertainty.
"""
from __future__ import annotations

from typing import Optional

from ..math_utils import linear_regression, normal_cdf

_MIN_TAIL = 12
_TAIL_MONTHS = 24
_MIN_SIGMA_PPM = 0.3


def projection(co2: dict) -> Optional[dict]:
    if not co2 or not co2.get("monthly"):
        return None
    series = co2["monthly"]
    cur_year = max(s["year"] for s in series)
    tail = series[-_TAIL_MONTHS:]
    if len(tail) < _MIN_TAIL:
        return None
    fit = linear_regression([s["decimal_date"] for s in tail], [s["ppm"] for s in tail])
    if fit is None:
        return None
    slope, intercept, sigma = fit
    sigma = max(sigma, _MIN_SIGMA_PPM)
    proj = intercept + slope * (cur_year + 1.0)
    return {
        "current_year": cur_year,
        "latest_ppm": series[-1]["ppm"],
        "ppm_per_year": round(slope, 3),
        "projected_year_end_ppm": round(proj, 2),
        "residual_std_ppm": round(sigma, 3),
    }


def threshold_probs(proj: Optional[dict],
                    thresholds_ppm: tuple[float, ...] = (424, 425, 426, 427, 428, 429, 430)) -> Optional[dict]:
    if not proj:
        return None
    mu = proj.get("projected_year_end_ppm")
    sigma = proj.get("residual_std_ppm") or 0.5
    if mu is None:
        return None
    out = [{"threshold_ppm": t,
            "p_at_or_above": round(1.0 - normal_cdf((t - mu) / sigma), 3)}
           for t in thresholds_ppm]
    return {"thresholds": out, "mu_ppm": mu, "sigma_ppm": sigma}


def backtest(co2: Optional[dict], n_years: int = 5) -> list[dict]:
    if not co2 or not co2.get("monthly"):
        return []
    series = co2["monthly"]
    cur_year = max(s["year"] for s in series)
    rows: list[dict] = []
    for target_year in range(cur_year - n_years, cur_year):
        mid = [s for s in series if s["year"] == target_year and s["month"] == 6]
        actual = [s for s in series if s["year"] == target_year and s["month"] == 12]
        if not mid or not actual:
            continue
        cutoff = mid[-1]["decimal_date"]
        tail = [s for s in series if s["decimal_date"] <= cutoff][-_TAIL_MONTHS:]
        if len(tail) < 12:
            continue
        fit = linear_regression([s["decimal_date"] for s in tail], [s["ppm"] for s in tail])
        if fit is None:
            continue
        slope, intercept, _ = fit
        proj = intercept + slope * (target_year + 1.0)
        rows.append({
            "year": target_year,
            "as_of": "Jun",
            "projected_year_end_ppm": round(proj, 2),
            "actual_dec_ppm": round(actual[-1]["ppm"], 2),
            "error_ppm": round(proj - actual[-1]["ppm"], 2),
        })
    return rows
