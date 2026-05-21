"""Year-end SF₆ projection.

Same 24-month linear regression as the other long-lived GHGs. SF₆ is the
smoothest of all — concentrations rise ~0.3 ppt/yr almost monotonically with
tiny seasonal cycle — so the σ floor is the tightest yet (0.05 ppt).
"""
from __future__ import annotations

from typing import Optional

from ..math_utils import linear_regression, normal_cdf

_MIN_TAIL = 12
_TAIL_MONTHS = 24
_MIN_SIGMA_PPT = 0.05


def projection(sf6: dict) -> Optional[dict]:
    if not sf6 or not sf6.get("monthly"):
        return None
    series = sf6["monthly"]
    cur_year = max(s["year"] for s in series)
    tail = series[-_TAIL_MONTHS:]
    if len(tail) < _MIN_TAIL:
        return None
    fit = linear_regression([s["decimal_date"] for s in tail], [s["ppt"] for s in tail])
    if fit is None:
        return None
    slope, intercept, sigma = fit
    sigma = max(sigma, _MIN_SIGMA_PPT)
    proj = intercept + slope * (cur_year + 1.0)
    return {
        "current_year": cur_year,
        "latest_ppt": series[-1]["ppt"],
        "ppt_per_year": round(slope, 3),
        "projected_year_end_ppt": round(proj, 3),
        "residual_std_ppt": round(sigma, 3),
    }


def threshold_probs(proj: Optional[dict],
                    thresholds_ppt: tuple[float, ...] = (11.0, 11.5, 12.0, 12.5, 13.0)) -> Optional[dict]:
    if not proj:
        return None
    mu = proj.get("projected_year_end_ppt")
    sigma = proj.get("residual_std_ppt") or 0.1
    if mu is None:
        return None
    out = [{"threshold_ppt": t,
            "p_at_or_above": round(1.0 - normal_cdf((t - mu) / sigma), 3)}
           for t in thresholds_ppt]
    return {"thresholds": out, "mu_ppt": mu, "sigma_ppt": sigma}


def backtest(sf6: Optional[dict], n_years: int = 5) -> list[dict]:
    if not sf6 or not sf6.get("monthly"):
        return []
    series = sf6["monthly"]
    cur_year = max(s["year"] for s in series)
    rows: list[dict] = []
    for target_year in range(cur_year - n_years, cur_year):
        mid = [s for s in series if s["year"] == target_year and s["month"] == 6]
        actual = [s for s in series if s["year"] == target_year and s["month"] == 12]
        if not mid or not actual:
            continue
        cutoff = mid[-1]["decimal_date"]
        tail = [s for s in series if s["decimal_date"] <= cutoff][-_TAIL_MONTHS:]
        if len(tail) < _MIN_TAIL:
            continue
        fit = linear_regression([s["decimal_date"] for s in tail], [s["ppt"] for s in tail])
        if fit is None:
            continue
        slope, intercept, _ = fit
        proj = intercept + slope * (target_year + 1.0)
        rows.append({
            "year": target_year,
            "as_of": "Jun",
            "projected_year_end_ppt": round(proj, 3),
            "actual_dec_ppt": round(actual[-1]["ppt"], 3),
            "error_ppt": round(proj - actual[-1]["ppt"], 3),
        })
    return rows
