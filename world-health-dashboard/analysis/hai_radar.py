"""HAI / AMR radar — combines WHO GLASS + WHO GASP + CDC ARPSP.

Three views over the same underlying data:

  1. Globe layer per indicator (default MRSA) — used by the HAI tab to
     color-code countries.
  2. Per-country drill-down — every available AMR indicator for a country
     plus latest year.
  3. C. auris US-state breakdown — the emerging-fungal surveillance feed
     from CDC ARPSP.
"""

from __future__ import annotations

import logging

from ingestion import cdc_arpsp, who_amr
from ingestion.country_codes import name_of

log = logging.getLogger(__name__)


def globe_layer(indicator_id: str = "amr_mrsa") -> dict:
    """Return {iso3: {value, year}} + percentiles for color scaling."""
    data = who_amr.fetch_indicator_data(indicator_id)
    if data.get("error"):
        return {"error": data["error"]}
    ind = data["indicator"]
    latest = data.get("latest", {})
    values = sorted([v["value"] for v in latest.values() if v.get("value") is not None])
    n = len(values)

    def _q(p: float) -> float | None:
        if n == 0:
            return None
        if n == 1:
            return values[0]
        idx = (n - 1) * p
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return values[lo] * (1 - frac) + values[hi] * frac

    return {
        "indicator": ind,
        "by_iso3": {iso: {"value": v["value"], "year": v["year"]}
                    for iso, v in latest.items()},
        "min": values[0] if values else None,
        "max": values[-1] if values else None,
        "p10": _q(0.10),
        "p50": _q(0.50),
        "p90": _q(0.90),
        "n": n,
        "fetched_at": data.get("fetched_at"),
        "stale": data.get("stale", False),
    }


def country_profile(iso3: str) -> dict:
    """Per-country drill-down: every WHO AMR indicator's latest value."""
    iso = iso3.upper()
    name = name_of(iso)
    if not name:
        return {"error": f"unknown iso3: {iso3}"}
    payload = who_amr.fetch_all()
    indicators_out: list[dict] = []
    for ind_id, data in payload.items():
        latest = data.get("latest", {}).get(iso)
        ind = data["indicator"]
        history = data.get("by_country", {}).get(iso, [])
        indicators_out.append({
            "id": ind_id,
            "name": ind["name"],
            "pathogen": ind["pathogen"],
            "antibiotic": ind["antibiotic"],
            "specimen": ind.get("specimen"),
            "source": ind["source"],
            "value": latest["value"] if latest else None,
            "year": latest["year"] if latest else None,
            "history": history,
        })
    return {
        "iso3": iso,
        "country": name,
        "indicators": indicators_out,
    }


def c_auris_summary() -> dict:
    """Wrap the CDC ARPSP latest summary with US-state percentages."""
    summary = cdc_arpsp.latest_summary()
    by_state = summary.get("by_state", {})
    # Reshape: {state: {drug: percent_resistant}}
    state_breakdown: dict[str, dict] = {}
    for state, rows in by_state.items():
        for r in rows:
            drug = r.get("drug")
            pct = r.get("percent_resistant")
            if pct is None or not drug:
                continue
            state_breakdown.setdefault(state, {})[drug] = {
                "percent_resistant": pct,
                "isolates": r.get("isolates"),
            }
    # National: drug -> percent
    national = {drug: r.get("percent_resistant") for drug, r in summary.get("national", {}).items()}
    return {
        "pathogen": "Candida auris",
        "year": summary.get("year"),
        "national_percent_resistant": national,
        "by_state": state_breakdown,
        "states_with_data": len(state_breakdown),
        "fetched_at": summary.get("fetched_at"),
    }


def overview() -> dict:
    """Top-level summary: indicator catalog + global counts + C. auris brief."""
    cat = who_amr.all_indicators()
    payload = who_amr.fetch_all()
    overview_indicators: list[dict] = []
    for ind in cat:
        d = payload.get(ind["id"], {})
        latest = d.get("latest", {})
        values = [v["value"] for v in latest.values() if v.get("value") is not None]
        years = [v["year"] for v in latest.values() if v.get("year") is not None]
        overview_indicators.append({
            **ind,
            "countries_reporting": len(latest),
            "latest_year": max(years) if years else None,
            "global_median": sorted(values)[len(values)//2] if values else None,
        })
    return {
        "indicators": overview_indicators,
        "c_auris": c_auris_summary(),
    }
