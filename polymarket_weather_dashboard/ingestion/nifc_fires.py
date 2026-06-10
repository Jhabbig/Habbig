"""NIFC wildfire ingestion - active US incidents + acres-burned.

The National Interagency Fire Center publishes active US wildfire incidents
through an ArcGIS REST FeatureService that's queryable as GeoJSON without
auth. We use the WFIGS Current Incident Locations layer:

    https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/
        WFIGS_Incident_Locations_Current/FeatureServer/0/query

Fields we read:
  - DailyAcres            best-known acreage at last update
  - DiscoveryAcres        size at first discovery
  - IncidentName          human-readable name
  - FireDiscoveryDateTime ISO timestamp of discovery
  - PercentContained      0-100
  - POOFips               place-of-origin FIPS (not used here, useful later)

The endpoint returns flat features. We sum DailyAcres for an "active acres"
metric, then project year-end acres burned by combining that with a
historical-rate prior. Markets typically ask "Will X acres burn in 2026?",
so the acres-pace projection is what we feed `analysis.acres_pace` and
`analysis.market_matcher`.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

WFIGS_CURRENT_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
)

# 2014-2024 NIFC YTD-acres-by-Dec-31 (in millions of acres).
# Source: NIFC annual statistics page. Used as a climatological prior so the
# year-end projection has a sensible band even when the live feed misses or
# hiccups - and so the UI can show "is this year on pace?" without waiting
# until December.
ANNUAL_ACRES_HISTORY: dict[int, float] = {
    2014: 3_595_613,
    2015: 10_125_149,
    2016: 5_509_995,
    2017: 10_026_086,
    2018: 8_767_492,
    2019: 4_664_364,
    2020: 10_122_336,
    2021: 7_125_643,
    2022: 7_534_403,
    2023: 2_693_910,
    2024: 8_924_884,
}


def _fetch_active_incidents() -> Optional[list[dict]]:
    params = {
        "where": "1=1",
        "outFields": "DailyAcres,DiscoveryAcres,IncidentName,FireDiscoveryDateTime,PercentContained,POOFips,IncidentTypeCategory",
        "f": "geojson",
        "resultRecordCount": "2000",
    }
    r = http_get(WFIGS_CURRENT_URL, params=params, timeout=30)
    if not r:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    feats = data.get("features") or []
    out: list[dict] = []
    for f in feats:
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        # Only include WF (Wildfire) incidents, not RX prescribed burns
        cat = (props.get("IncidentTypeCategory") or "").upper()
        if cat and cat not in {"WF", "WFU"}:
            continue
        try:
            acres = float(props.get("DailyAcres") or 0)
        except (TypeError, ValueError):
            acres = 0.0
        try:
            disc_acres = float(props.get("DiscoveryAcres") or 0)
        except (TypeError, ValueError):
            disc_acres = 0.0
        out.append({
            "name": props.get("IncidentName"),
            "daily_acres": round(acres, 1),
            "discovery_acres": round(disc_acres, 1),
            "discovery_iso": props.get("FireDiscoveryDateTime"),
            "percent_contained": props.get("PercentContained"),
            "lat": coords[1] if len(coords) >= 2 else None,
            "lon": coords[0] if len(coords) >= 1 else None,
        })
    return out


def active_incidents() -> dict:
    hit = _cache.get("nifc_active", ttl_s=900)  # 15 min
    if hit is not None:
        return hit
    rows = _fetch_active_incidents()
    if rows is None:
        return {"error": "NIFC fetch failed", "incidents": [], "count": 0,
                "active_acres_total": 0}
    rows.sort(key=lambda r: r.get("daily_acres") or 0, reverse=True)
    total_acres = round(sum(r.get("daily_acres") or 0 for r in rows), 1)
    out = {
        "source": "NIFC WFIGS Incident Locations Current (ArcGIS GeoJSON)",
        "count": len(rows),
        "active_acres_total": total_acres,
        "incidents": rows[:100],   # cap on the wire
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("nifc_active", out)
    return out


def historical_mean_acres() -> float:
    """Mean year-end acres burned across the cached climo years."""
    if not ANNUAL_ACRES_HISTORY:
        return 0.0
    return sum(ANNUAL_ACRES_HISTORY.values()) / len(ANNUAL_ACRES_HISTORY)


def historical_std_acres() -> float:
    if len(ANNUAL_ACRES_HISTORY) < 2:
        return 0.0
    mean = historical_mean_acres()
    sq = sum((v - mean) ** 2 for v in ANNUAL_ACRES_HISTORY.values())
    return math.sqrt(sq / (len(ANNUAL_ACRES_HISTORY) - 1))


def acres_burned_year_end_projection() -> dict:
    """Project year-end acres burned using a calendar-aware extrapolation.

    Approach:

      * Active acres (NIFC live) are a *lower bound* on year-to-date acres
        burned - closed fires aren't counted. We treat the active total as
        the floor.
      * Add a calendar-progress prior: at day d of year, by Dec 31 we'd
        expect cumulative acres to reach roughly ``mean × shape(d)`` where
        ``shape`` is a sigmoid centred on day-220 (early August). That is a
        decent fit to historical NIFC daily-cumulative curves.
      * Year-end estimate = max(active_lower_bound, calendar_prior).
      * Sigma = historical std × scaling for the remaining-year fraction.
    """
    active = active_incidents()
    if active.get("error"):
        return {"error": active["error"]}
    today = datetime.now(timezone.utc).date()
    year = today.year
    days_in_year = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    days_into_year = (today - date(year, 1, 1)).days + 1

    # Sigmoid approximation of the cumulative-acres-by-DOY curve: mid = 220, slope = 25
    def _shape(d: int) -> float:
        return 1.0 / (1.0 + math.exp(-(d - 220.0) / 25.0))

    cum_today_fraction = _shape(days_into_year)
    cum_year_end_fraction = _shape(days_in_year)
    calendar_progress_pct = cum_today_fraction / cum_year_end_fraction
    mean_annual = historical_mean_acres()
    sigma_annual = historical_std_acres()
    # Calendar prior: if "we're 60% of the way through the cumulative curve",
    # then year-end = current_active_floor / 0.6 (if floor > 0), else
    # mean_annual * (we'll-see-everything-by-year-end fraction).
    active_floor = active.get("active_acres_total") or 0.0
    if calendar_progress_pct > 0:
        floor_implied = active_floor / calendar_progress_pct
    else:
        floor_implied = 0.0
    calendar_prior = mean_annual
    projection = max(floor_implied, calendar_prior * 0.5 + active_floor)
    projection = max(projection, active_floor)

    # Sigma scales with how much of the year is left under the shape model
    remaining_fraction = max(0.0, 1.0 - calendar_progress_pct)
    sigma = sigma_annual * (0.5 + 0.5 * remaining_fraction)

    out = {
        "source": "NIFC WFIGS active + 2014-2024 NIFC annual climatology",
        "year": year,
        "as_of": today.isoformat(),
        "days_into_year": days_into_year,
        "active_acres_total": active_floor,
        "active_incident_count": active.get("count", 0),
        "calendar_progress_pct": round(calendar_progress_pct * 100, 1),
        "historical_mean_annual_acres": round(mean_annual),
        "historical_std_annual_acres": round(sigma_annual),
        "projected_year_end_acres": round(projection),
        "projection_sigma_acres": round(sigma),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    return out


def p_year_end_acres_at_least(proj: dict, threshold_acres: float) -> Optional[float]:
    """P(year-end acres >= T) under N(projection, sigma)."""
    if not proj or proj.get("error"):
        return None
    mu = proj.get("projected_year_end_acres")
    sigma = proj.get("projection_sigma_acres")
    if mu is None or not sigma:
        return None
    z = (threshold_acres - mu) / max(float(sigma), 1.0)
    # Normal-CDF complement
    return 0.5 * math.erfc(z / math.sqrt(2))


if __name__ == "__main__":
    import json
    print(json.dumps(active_incidents(), indent=2)[:1500])
    print(json.dumps(acres_burned_year_end_projection(), indent=2))
