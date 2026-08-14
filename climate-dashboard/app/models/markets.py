"""Match Polymarket questions to climate models, then attach a model probability + edge.

Markets without a discoverable target (free-form questions) pass through
unscored — they still appear in the table but with an empty model column.
"""
from __future__ import annotations

import re
from typing import Optional

from ..math_utils import normal_cdf

_RE_LT = re.compile(r"(?:less than|below|under)\s*([\d.]+)\s*m", re.I)
_RE_GE = re.compile(r"(?:at least|more than|above|over|exceed[a-z]*)\s*([\d.]+)\s*m", re.I)
_RE_BETWEEN = re.compile(r"between\s*([\d.]+)\s*m?\s*(?:&|and|to|-)\s*([\d.]+)\s*m", re.I)

_RE_ANOMALY_GE = re.compile(r"(?:above|exceed[a-z]*|at least|over|greater than|more than)\s*\+?\s*([\d.]+)\s*°?\s*c", re.I)
_RE_ANOMALY_LT = re.compile(r"(?:below|under|less than)\s*\+?\s*([\d.]+)\s*°?\s*c", re.I)


def ice_min_market_p(question: str, proj: dict) -> Optional[float]:
    """P(min sea ice extent matches threshold) under N(projected, residual_std)."""
    mu = proj["projected_min_mkm2"]
    sigma = proj["residual_std_mkm2"]
    if sigma <= 0:
        return None
    q = question.lower()
    m = _RE_BETWEEN.search(q)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return normal_cdf((hi - mu) / sigma) - normal_cdf((lo - mu) / sigma)
    m = _RE_LT.search(q)
    if m:
        return normal_cdf((float(m.group(1)) - mu) / sigma)
    m = _RE_GE.search(q)
    if m:
        return 1.0 - normal_cdf((float(m.group(1)) - mu) / sigma)
    return None


def temperature_anomaly_market_p(question: str, proj: dict) -> Optional[float]:
    """For markets like 'Will 2026 global anomaly be above 1.5°C?'"""
    mu = proj.get("projected_annual_anomaly_c")
    sigma = max(proj.get("drift_std_c") or 0.05, 0.03)
    if mu is None:
        return None
    q = question.lower()
    m = _RE_ANOMALY_GE.search(q)
    if m:
        thr = float(m.group(1))
        if 0.5 <= thr <= 3.0:
            return 1.0 - normal_cdf((thr - mu) / sigma)
    m = _RE_ANOMALY_LT.search(q)
    if m:
        thr = float(m.group(1))
        if 0.5 <= thr <= 3.0:
            return normal_cdf((thr - mu) / sigma)
    return None


def co2_threshold_market_p(question: str, proj: dict) -> Optional[float]:
    mu = proj.get("projected_year_end_ppm")
    sigma = max(proj.get("residual_std_ppm") or 0.5, 0.3)
    if mu is None:
        return None
    q = question.lower()
    m = re.search(r"(?:exceed[a-z]*|above|over|more than|at least|reach[a-z]*)\s*(\d{3}(?:\.\d+)?)\s*ppm", q)
    if m:
        return 1.0 - normal_cdf((float(m.group(1)) - mu) / sigma)
    m = re.search(r"(?:below|under|less than)\s*(\d{3}(?:\.\d+)?)\s*ppm", q)
    if m:
        return normal_cdf((float(m.group(1)) - mu) / sigma)
    return None


def methane_threshold_market_p(question: str, proj: dict) -> Optional[float]:
    """Methane thresholds in markets are typically expressed in ppb (4 digits)
    or occasionally ppm (1.9-2.0 with 'ppm' explicit). We accept both."""
    mu = proj.get("projected_year_end_ppb")
    sigma = max(proj.get("residual_std_ppb") or 5.0, 2.0)
    if mu is None:
        return None
    q = question.lower()
    m = re.search(r"(?:exceed[a-z]*|above|over|more than|at least|reach[a-z]*)\s*(\d{4}(?:\.\d+)?)\s*ppb", q)
    if m:
        return 1.0 - normal_cdf((float(m.group(1)) - mu) / sigma)
    m = re.search(r"(?:below|under|less than)\s*(\d{4}(?:\.\d+)?)\s*ppb", q)
    if m:
        return normal_cdf((float(m.group(1)) - mu) / sigma)
    m = re.search(r"(?:exceed[a-z]*|above|over|more than|at least)\s*([12](?:\.\d{1,3})?)\s*ppm", q)
    if m and "methane" in q:
        thr = float(m.group(1)) * 1000.0
        return 1.0 - normal_cdf((thr - mu) / sigma)
    return None


def edges_for_markets(markets: list[dict],
                      gistemp_proj: Optional[dict],
                      co2_proj: Optional[dict],
                      arctic_proj: Optional[dict] = None,
                      antarctic_proj: Optional[dict] = None,
                      methane_proj: Optional[dict] = None) -> list[dict]:
    """Attach a model probability + edge (in pp) to markets we can score."""
    out = []
    for m in markets:
        title = ((m.get("_event_title") or "") + " " + (m.get("question") or "")).strip()
        tl = title.lower()
        try:
            implied = float(m.get("lastTradePrice") or m.get("bestBid") or 0)
        except (ValueError, TypeError):
            implied = None
        model_p: Optional[float] = None
        rationale = ""

        if gistemp_proj and ("warmest year" in tl or "hottest year" in tl
                             or ("record" in tl and "temperature" in tl)):
            model_p = gistemp_proj.get("p_breaks_record")
            rationale = (f"YTD {gistemp_proj['ytd_anomaly_c']}°C → projected "
                         f"{gistemp_proj['projected_annual_anomaly_c']}°C vs record "
                         f"{gistemp_proj['current_record']['anomaly_c']}°C "
                         f"({gistemp_proj['current_record']['year']})")

        if model_p is None and gistemp_proj and ("anomaly" in tl or "global temperature" in tl
                                                  or "global average" in tl or "1.5" in tl
                                                  or "warming" in tl):
            p = temperature_anomaly_market_p(title, gistemp_proj)
            if p is not None:
                model_p = max(0.0, min(1.0, p))
                rationale = (f"N(μ={gistemp_proj['projected_annual_anomaly_c']}°C, "
                             f"σ={gistemp_proj['drift_std_c']}°C) projection")

        if model_p is None and antarctic_proj and ("antarctic" in tl
                                                    and ("sea ice" in tl or "ice extent" in tl)):
            p = ice_min_market_p(title, antarctic_proj)
            if p is not None:
                model_p = max(0.0, min(1.0, p))
                rationale = (f"Antarctic trend → {antarctic_proj['projected_min_mkm2']} Mkm² "
                             f"(σ={antarctic_proj['residual_std_mkm2']}, "
                             f"{antarctic_proj['trend_mkm2_per_year']:+.3f}/yr)")

        if model_p is None and arctic_proj and ("arctic sea ice" in tl
                                                  or "minimum arctic" in tl
                                                  or ("sea ice" in tl and "antarctic" not in tl)):
            p = ice_min_market_p(title, arctic_proj)
            if p is not None:
                model_p = max(0.0, min(1.0, p))
                rationale = (f"Trend → {arctic_proj['projected_min_mkm2']} Mkm² "
                             f"(σ={arctic_proj['residual_std_mkm2']}, "
                             f"{arctic_proj['trend_mkm2_per_year']:+.3f}/yr)")

        if model_p is None and co2_proj and ("co2" in tl or "carbon dioxide" in tl or "ppm" in tl):
            p = co2_threshold_market_p(title, co2_proj)
            if p is not None:
                model_p = max(0.0, min(1.0, p))
                rationale = (f"N(μ={co2_proj['projected_year_end_ppm']} ppm, "
                             f"σ={co2_proj['residual_std_ppm']} ppm), "
                             f"+{co2_proj['ppm_per_year']}/yr")

        if model_p is None and methane_proj and ("methane" in tl or "ch4" in tl or "ppb" in tl):
            p = methane_threshold_market_p(title, methane_proj)
            if p is not None:
                model_p = max(0.0, min(1.0, p))
                rationale = (f"N(μ={methane_proj['projected_year_end_ppb']} ppb, "
                             f"σ={methane_proj['residual_std_ppb']} ppb), "
                             f"+{methane_proj['ppb_per_year']}/yr")

        edge = (round((model_p - implied) * 100, 1)
                if implied is not None and model_p is not None else None)
        out.append({
            **m,
            "_implied_p": implied,
            "_model_p": round(model_p, 3) if model_p is not None else None,
            "_edge_pp": edge,
            "_rationale": rationale,
        })
    return out
