"""NWS SPC convective outlook (D1, D2, D3) - tornado/hail/wind risk.

SPC publishes a daily probabilistic outlook for severe convective weather.
The categorical risk levels are:

  TSTM (general thunderstorm), MRGL (marginal), SLGT (slight),
  ENH (enhanced), MDT (moderate), HIGH

For day-of (D1) and the next two days (D2, D3) SPC also publishes
tornado-probability isobars (2%, 5%, 10%, 15%, 30%, 45%, 60%).

We use the GeoJSON snapshots SPC publishes for each forecast day:

    https://www.spc.noaa.gov/products/outlook/day1otlk_cat.lyr.geojson
    https://www.spc.noaa.gov/products/outlook/day2otlk_cat.lyr.geojson
    https://www.spc.noaa.gov/products/outlook/day3otlk_cat.lyr.geojson

The GeoJSON returns one feature per categorical-risk polygon, with the
``LABEL2`` property carrying the category name. We don't render the
polygons; we just extract the highest-tier category currently in effect
and a count of states/regions affected (using the polygon's bounding-box
overlap with state boundaries is overkill for v1).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

CATEGORY_RANK = ["TSTM", "MRGL", "SLGT", "ENH", "MDT", "HIGH"]

URLS = {
    "day1": "https://www.spc.noaa.gov/products/outlook/day1otlk_cat.lyr.geojson",
    "day2": "https://www.spc.noaa.gov/products/outlook/day2otlk_cat.lyr.geojson",
    "day3": "https://www.spc.noaa.gov/products/outlook/day3otlk_cat.lyr.geojson",
}


def _summarise(day: str) -> Optional[dict]:
    r = http_get(URLS[day], timeout=20)
    if not r:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    feats = data.get("features") or []
    cats: list[str] = []
    for f in feats:
        props = f.get("properties") or {}
        label = props.get("LABEL2") or props.get("LABEL") or props.get("Name") or ""
        if label:
            cats.append(label.strip().upper())
    # Highest-rank category in effect
    rank = -1
    top = None
    for c in cats:
        try:
            r_ = CATEGORY_RANK.index(c)
        except ValueError:
            continue
        if r_ > rank:
            rank = r_
            top = c
    return {
        "day": day,
        "category_count_in_effect": len(cats),
        "highest_category": top,
        "categories": cats,
    }


def outlooks() -> dict:
    hit = _cache.get("spc_outlooks", ttl_s=1800)  # 30 min
    if hit is not None:
        return hit
    days: dict[str, dict] = {}
    for d in ("day1", "day2", "day3"):
        s = _summarise(d)
        if s:
            days[d] = s
    if not days:
        return {"error": "SPC outlook fetch failed", "days": {}}
    # Aggregate "biggest risk in 3-day horizon"
    horizon_top = None
    horizon_rank = -1
    for d, s in days.items():
        c = s.get("highest_category")
        if not c:
            continue
        try:
            r_ = CATEGORY_RANK.index(c)
        except ValueError:
            continue
        if r_ > horizon_rank:
            horizon_rank = r_
            horizon_top = (d, c)
    out = {
        "source": "SPC convective outlook day1/2/3 GeoJSON",
        "days": days,
        "horizon_highest_day": horizon_top[0] if horizon_top else None,
        "horizon_highest_category": horizon_top[1] if horizon_top else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("spc_outlooks", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(outlooks(), indent=2))
