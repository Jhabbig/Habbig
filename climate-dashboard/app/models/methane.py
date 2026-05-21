"""Year-end CH₄ projection. Same shape as CO₂ but noisier data → higher σ floor."""
from __future__ import annotations

from typing import Optional

from ..math_utils import linear_regression, normal_cdf

_MIN_TAIL = 12
_TAIL_MONTHS = 24
_MIN_SIGMA_PPB = 2.0


def projection(ch4: dict) -> Optional[dict]:
    if not ch4 or not ch4.get("monthly"):
        return None
    series = ch4["monthly"]
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
        "ppb_per_year": round(slope, 2),
        "projected_year_end_ppb": round(proj, 2),
        "residual_std_ppb": round(sigma, 2),
    }


def threshold_probs(proj: Optional[dict],
                    thresholds_ppb: tuple[float, ...] = (1930, 1940, 1950, 1960, 1970, 1980, 1990, 2000)) -> Optional[dict]:
    if not proj:
        return None
    mu = proj.get("projected_year_end_ppb")
    sigma = proj.get("residual_std_ppb") or 5.0
    if mu is None:
        return None
    out = [{"threshold_ppb": t,
            "p_at_or_above": round(1.0 - normal_cdf((t - mu) / sigma), 3)}
           for t in thresholds_ppb]
    return {"thresholds": out, "mu_ppb": mu, "sigma_ppb": sigma}


def backtest(ch4: Optional[dict], n_years: int = 5) -> list[dict]:
    if not ch4 or not ch4.get("monthly"):
        return []
    series = ch4["monthly"]
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
            "projected_year_end_ppb": round(proj, 2),
            "actual_dec_ppb": round(actual[-1]["ppb"], 2),
            "error_ppb": round(proj - actual[-1]["ppb"], 2),
        })
    return rows
