"""NHC active tropical cyclones + Atlantic season projection.

  * ``active_storms()`` reads the NHC's CurrentStorms.json feed - the same
    feed that powers nhc.noaa.gov - and surfaces every named storm currently
    being tracked by the National Hurricane Center, plus its Pacific cousin
    if there are any.

  * ``atlantic_season_projection()`` extrapolates the year-end Atlantic named
    storm count from YTD storms (named so far this year) using the seasonal
    climatology the NHC publishes in its annual outlooks.

NHC's CurrentStorms.json is the cleanest live feed I've found - no scraping
required. Their JSON is a flat array of currently-active named systems with
Saffir-Simpson category, sustained winds, and last-fix lat/lon.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

NHC_CURRENT_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"

# Median + average climatology from NOAA's 1991-2020 baseline.
# Atlantic season runs Jun 1 - Nov 30 (so 153 days), but we treat year-end
# arrival deadline as Dec 31 since markets are typically year-anchored.
ATLANTIC_CLIMO = {
    "median_named_per_year": 14,
    "median_hurricanes_per_year": 7,
    "median_major_per_year": 3,
    "season_start": (6, 1),
    "season_end": (11, 30),
}


def _classify_intensity(sustained_kt: Optional[float]) -> str:
    if sustained_kt is None:
        return "Unknown"
    kt = float(sustained_kt)
    if kt >= 137: return "Cat 5"
    if kt >= 113: return "Cat 4"
    if kt >= 96:  return "Cat 3"
    if kt >= 83:  return "Cat 2"
    if kt >= 64:  return "Cat 1"
    if kt >= 34:  return "Tropical Storm"
    return "Tropical Depression"


def active_storms() -> dict:
    hit = _cache.get("nhc_active", ttl_s=600)  # 10 min
    if hit is not None:
        return hit
    r = http_get(NHC_CURRENT_URL, timeout=20)
    if not r:
        return {"error": "NHC fetch failed", "storms": [], "count": 0}
    try:
        data = r.json()
    except ValueError:
        return {"error": "NHC parse failed", "storms": [], "count": 0}
    raw = data.get("activeStorms") or data if isinstance(data, list) else data.get("activeStorms") or []
    if not isinstance(raw, list):
        raw = []
    storms: list[dict] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        wind = s.get("intensity") or s.get("intensityKt") or s.get("maxSustainedWindMph")
        try:
            wind_f = float(wind) if wind is not None else None
        except (TypeError, ValueError):
            wind_f = None
        # NHC publishes intensity in kt for tropical advisories
        storms.append({
            "id": s.get("id") or s.get("binNumber") or s.get("stormNumber"),
            "name": s.get("name"),
            "classification": s.get("classification") or _classify_intensity(wind_f),
            "intensity_kt": wind_f,
            "movement": s.get("movement"),
            "lat": s.get("latitudeNumeric") or s.get("lat"),
            "lon": s.get("longitudeNumeric") or s.get("lon"),
            "basin": s.get("basin") or s.get("basinId"),
            "last_update": s.get("lastUpdate") or s.get("issuance"),
            "public_advisory_url": (s.get("publicAdvisory") or {}).get("url") if isinstance(s.get("publicAdvisory"), dict) else None,
        })
    out = {
        "source": "NHC CurrentStorms.json",
        "count": len(storms),
        "storms": storms,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("nhc_active", out)
    return out


def _named_storms_ytd_atlantic(year: int) -> Optional[int]:
    """Count named-storm bulletins in the Atlantic basin so far this year.

    Without paid API access we approximate using the NHC's archive RSS - but
    that is heavy. As a pragmatic placeholder we read the count of currently-
    active Atlantic systems, which equals "named storms still being tracked"
    rather than the full season-to-date. Callers should treat this as a
    lower-bound view; a future iteration can swap in the NHC archive parser.
    """
    storms = active_storms()
    if storms.get("error"):
        return None
    return sum(
        1 for s in storms.get("storms", [])
        if (s.get("basin") or "").upper() in {"AL", "ATLANTIC", "AT"}
    )


def atlantic_season_projection() -> dict:
    """Extrapolate year-end Atlantic named storm count from YTD + climo.

    Conservative model: if we are inside the season, project linearly to the
    season end based on calendar progress; if we are post-season, the YTD
    count IS the year-end count. Add a Poisson lambda over the remaining
    season days using the climatological annual rate as the prior, so we can
    compute "P(>= N storms)" on Polymarket markets.
    """
    hit = _cache.get("nhc_season_proj", ttl_s=1800)
    if hit is not None:
        return hit
    today = datetime.now(timezone.utc).date()
    year = today.year
    s_start = date(year, *ATLANTIC_CLIMO["season_start"])
    s_end = date(year, *ATLANTIC_CLIMO["season_end"])
    season_days = (s_end - s_start).days + 1
    if today < s_start:
        days_into_season = 0
        days_remaining_season = season_days
    elif today > s_end:
        days_into_season = season_days
        days_remaining_season = 0
    else:
        days_into_season = (today - s_start).days + 1
        days_remaining_season = season_days - days_into_season
    ytd_active = _named_storms_ytd_atlantic(year) or 0
    climo_annual = ATLANTIC_CLIMO["median_named_per_year"]
    # Lambda for remaining season under climo rate. We DON'T extrapolate from
    # YTD storms here because the active-storm count is a lower bound (storms
    # closed before today aren't counted); leaning on the climo prior is more
    # honest until we wire in the NHC archive.
    lam_remaining = climo_annual * (days_remaining_season / season_days)
    out = {
        "source": "NHC CurrentStorms + 1991-2020 Atlantic climo",
        "year": year,
        "as_of": today.isoformat(),
        "season_start": s_start.isoformat(),
        "season_end": s_end.isoformat(),
        "season_days": season_days,
        "days_into_season": days_into_season,
        "days_remaining_season": days_remaining_season,
        "active_named_storms_ytd_lower_bound": ytd_active,
        "climatological_annual_named": climo_annual,
        "lambda_remaining": round(lam_remaining, 2),
        "projected_year_end_count": int(round(ytd_active + lam_remaining)),
        "caveat": "YTD counts only currently-active storms (lower bound). Full-season count requires NHC archive parsing.",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("nhc_season_proj", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(active_storms(), indent=2))
    print(json.dumps(atlantic_season_projection(), indent=2))
