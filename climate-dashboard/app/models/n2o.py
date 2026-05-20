"""Year-end N₂O projection.

Same 24-month linear regression as CO₂/CH₄. N₂O rises ~1 ppb/yr and is very
smooth, so the residual std floor is the tightest of the three (0.3 ppb).
"""
from __future__ import annotations

from typing import Optional

from ..math_utils import linear_regression, normal_cdf

_MIN_TAIL = 6
_TAIL_MONTHS = 24
_MIN_SIGMA_PPB = 0.3


def projection(n2o: dict) -> Optional[dict]:
    if not n2o or not n2o.get("monthly"):
        return None
    series = n2o["monthly"]
    cur_year = max(s["year"] for s in series)
    tail = series[-_TAIL_MONTHS:]
    if len(tail) < _MIN_TAIL:
        return None
    fit = linear_regression([s["decimal_date"] for s in tail], [s["ppb"] for s in tail])
    if fit is None:
        return None
    slope, intercept, sigma = fit
    sigma = max(sigma, _MIN_SIGMA_PPB)
    proj = intercept + slope * (cur_year + 1.0)
    return {
        "current_year": cur_year,
        "latest_ppb": series[-1]["ppb"],
        "ppb_per_year": round(slope, 3),
        "projected_year_end_ppb": round(proj, 3),
        "residual_std_ppb": round(sigma, 3),
    }


def threshold_probs(proj: Optional[dict],
                    thresholds_ppb: tuple[float, ...] = (337, 338, 339, 340, 341, 342, 343)) -> Optional[dict]:
    if not proj:
        return None
    mu = proj.get("projected_year_end_ppb")
    sigma = proj.get("residual_std_ppb") or 0.5
    if mu is None:
        return None
    out = [{"threshold_ppb": t,
            "p_at_or_above": round(1.0 - normal_cdf((t - mu) / sigma), 3)}
           for t in thresholds_ppb]
    return {"thresholds": out, "mu_ppb": mu, "sigma_ppb": sigma}


def backtest(n2o: Optional[dict], n_years: int = 5) -> list[dict]:
    if not n2o or not n2o.get("monthly"):
        return []
    series = n2o["monthly"]
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
        fit = linear_regression([s["decimal_date"] for s in tail], [s["ppb"] for s in tail])
        if fit is None:
            continue
        slope, intercept, _ = fit
        proj = intercept + slope * (target_year + 1.0)
        rows.append({
            "year": target_year,
            "as_of": "Jun",
            "projected_year_end_ppb": round(proj, 3),
            "actual_dec_ppb": round(actual[-1]["ppb"], 3),
            "error_ppb": round(proj - actual[-1]["ppb"], 3),
        })
    return rows
