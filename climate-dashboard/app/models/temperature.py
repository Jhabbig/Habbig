"""Year-end temperature anomaly model.

Project this year's annual mean by combining the year-to-date mean with the
historical drift (average of (annual_mean − YTD_mean) across past years).
P(new record) follows from the residual std of that drift under a normal CDF.
"""
from __future__ import annotations

import math
from typing import Optional

from ..math_utils import normal_cdf


def projection(gistemp: dict) -> Optional[dict]:
    if not gistemp or not gistemp.get("monthly"):
        return None
    monthly = gistemp["monthly"]
    annual = gistemp.get("annual") or []
    if not annual:
        return None
    cur_year = max(m["year"] for m in monthly)
    cur_months = sorted([m for m in monthly if m["year"] == cur_year], key=lambda x: x["month"])
    if not cur_months:
        return None
    n = len(cur_months)
    ytd_mean = sum(m["anomaly_c"] for m in cur_months) / n

    diffs = []
    for y in [a["year"] for a in annual if a["year"] != cur_year]:
        ms = sorted([m for m in monthly if m["year"] == y], key=lambda x: x["month"])
        if len(ms) < 12:
            continue
        ytd_y = sum(m["anomaly_c"] for m in ms[:n]) / n
        ann_y = sum(m["anomaly_c"] for m in ms) / 12
        diffs.append(ann_y - ytd_y)
    if not diffs:
        return None
    drift = sum(diffs) / len(diffs)
    drift_std = math.sqrt(sum((d - drift) ** 2 for d in diffs) / len(diffs)) if len(diffs) > 1 else 0.05
    proj = round(ytd_mean + drift, 3)

    # Exclude the current year from the record candidates — otherwise the
    # moment GISTEMP publishes the J-D annual mean for ``cur_year``, that
    # value becomes the "current record" and the model trivially predicts a
    # 50/50 chance of breaking it.
    prior = [a for a in annual if a["year"] != cur_year]
    record = max(prior, key=lambda a: a["anomaly_c"]) if prior else annual[-1]
    p_breaks_record = normal_cdf((proj - record["anomaly_c"]) / max(drift_std, 0.01))

    return {
        "current_year": cur_year,
        "months_observed": n,
        "ytd_anomaly_c": round(ytd_mean, 3),
        "drift_to_year_end_c": round(drift, 3),
        "drift_std_c": round(drift_std, 3),
        "projected_annual_anomaly_c": proj,
        "current_record": record,
        "p_breaks_record": round(p_breaks_record, 3),
    }


def threshold_probs(proj: Optional[dict],
                    thresholds_c: tuple[float, ...] = (1.3, 1.4, 1.5, 1.6, 1.7, 1.8)) -> Optional[dict]:
    if not proj:
        return None
    mu = proj.get("projected_annual_anomaly_c")
    sigma = max(proj.get("drift_std_c") or 0.05, 0.03)
    if mu is None:
        return None
    out = [{"threshold_c": t,
            "p_at_or_above": round(1.0 - normal_cdf((t - mu) / sigma), 3)}
           for t in thresholds_c]
    return {"thresholds": out, "mu_c": mu, "sigma_c": round(sigma, 3)}


def backtest(gistemp: Optional[dict], n_years: int = 5) -> list[dict]:
    if not gistemp or not gistemp.get("monthly") or not gistemp.get("annual"):
        return []
    monthly = gistemp["monthly"]
    annual = {a["year"]: a["anomaly_c"] for a in gistemp["annual"]}
    cur_year = max(m["year"] for m in monthly)
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    rows: list[dict] = []
    for target_year in range(cur_year - n_years, cur_year):
        if target_year not in annual:
            continue
        yr_months = sorted([m for m in monthly if m["year"] == target_year], key=lambda x: x["month"])
        if len(yr_months) < 12:
            continue
        as_of = 6
        ytd = sum(m["anomaly_c"] for m in yr_months[:as_of]) / as_of
        diffs = []
        for y in [a for a in annual if a < target_year]:
            ms = sorted([m for m in monthly if m["year"] == y], key=lambda x: x["month"])
            if len(ms) < 12:
                continue
            ytd_y = sum(m["anomaly_c"] for m in ms[:as_of]) / as_of
            ann_y = sum(m["anomaly_c"] for m in ms) / 12
            diffs.append(ann_y - ytd_y)
        if not diffs:
            continue
        drift = sum(diffs) / len(diffs)
        proj = round(ytd + drift, 3)
        actual = annual[target_year]
        rows.append({
            "year": target_year,
            "as_of": months[as_of - 1],
            "projected_c": proj,
            "actual_c": round(actual, 3),
            "error_c": round(proj - actual, 3),
        })
    return rows
