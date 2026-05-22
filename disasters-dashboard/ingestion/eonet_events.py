"""NASA EONET (Earth Observations Natural Event Tracker) feed.

EONET aggregates open natural events from a dozen sources (NIFC, USGS,
SIVolcano, NOAA, IDMC, etc.) into a single JSON feed. Free, no key.

Categories: wildfires, severeStorms, volcanoes, drought, dustHaze, earthquakes,
floods, landslides, manmade, seaLakeIce, snow, tempExtremes, waterColor.

We use it for two things:

  1. Live "active threats" panels (open events grouped by category).
  2. Year-end count projections for each category, based on YTD event count
     and a 5-year same-category baseline, plus a Poisson-tail probability
     that the season exceeds the threshold count.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

EONET_BASE = "https://eonet.gsfc.nasa.gov/api/v3/events"

KNOWN_CATEGORIES: tuple[str, ...] = (
    "wildfires", "severeStorms", "volcanoes", "drought", "floods",
    "landslides", "earthquakes", "tempExtremes", "seaLakeIce",
)


def _fetch_events(*, category: Optional[str], status: str, days: int, limit: int = 500) -> Optional[list[dict]]:
    params = {
        "status": status,        # "open" | "closed" | "all"
        "limit": str(limit),
        "days": str(days),
    }
    if category and category != "all":
        params["category"] = category
    r = http_get(EONET_BASE, params=params, timeout=20)
    if not r:
        return None
    try:
        return r.json().get("events") or []
    except ValueError:
        return None


def _summarise(ev: dict) -> dict:
    """Strip an EONET event down to the fields the UI actually consumes."""
    geoms = ev.get("geometry") or []
    last_geom = geoms[-1] if geoms else {}
    coords = last_geom.get("coordinates") or []
    cats = ev.get("categories") or []
    return {
        "id": ev.get("id"),
        "title": ev.get("title"),
        "link": ev.get("link"),
        "categories": [c.get("title") for c in cats if isinstance(c, dict)],
        "category_ids": [c.get("id") for c in cats if isinstance(c, dict)],
        "closed": ev.get("closed"),
        "date": last_geom.get("date"),
        "lon": coords[0] if len(coords) >= 1 and isinstance(coords[0], (int, float)) else None,
        "lat": coords[1] if len(coords) >= 2 and isinstance(coords[1], (int, float)) else None,
    }


def open_events(category: str = "all", days: int = 30) -> dict:
    cache_key = f"eonet_open_{category}_{days}"
    hit = _cache.get(cache_key, ttl_s=600)  # 10 min
    if hit is not None:
        return hit
    events = _fetch_events(category=category, status="open", days=days)
    if events is None:
        return {"error": "EONET fetch failed", "events": [], "count": 0}
    summary = [_summarise(e) for e in events]
    by_category: dict[str, int] = {}
    for e in summary:
        for cid in e.get("category_ids") or []:
            by_category[cid] = by_category.get(cid, 0) + 1
    out = {
        "source": "NASA EONET v3 (events?status=open)",
        "category": category,
        "window_days": days,
        "count": len(summary),
        "events": summary,
        "by_category": by_category,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def _ytd_event_count(category: str, year: int) -> Optional[int]:
    today = datetime.now(timezone.utc).date()
    days_into_year = (today - date(year, 1, 1)).days + 1
    events = _fetch_events(category=category, status="all", days=days_into_year)
    if events is None:
        return None
    cutoff = date(year, 1, 1)
    count = 0
    for e in events:
        geoms = e.get("geometry") or []
        if not geoms:
            continue
        first = geoms[0].get("date") or ""
        try:
            d = datetime.fromisoformat(first.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            continue
        if d >= cutoff:
            count += 1
    return count


def year_end_count_projection(category: str = "wildfires") -> dict:
    """Project year-end EONET event count for the given category.

    Uses YTD-implied rate-per-day to extrapolate to year-end, plus a Poisson
    lambda for the remaining days so callers can compute "P(>= N events)".
    """
    if category not in KNOWN_CATEGORIES:
        return {"error": f"unknown category {category}"}
    cache_key = f"eonet_proj_{category}"
    hit = _cache.get(cache_key, ttl_s=1800)  # 30 min
    if hit is not None:
        return hit
    today = datetime.now(timezone.utc).date()
    year = today.year
    days_into_year = (today - date(year, 1, 1)).days + 1
    days_in_year = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    days_remaining = days_in_year - days_into_year
    ytd = _ytd_event_count(category, year)
    if ytd is None:
        return {"error": "EONET fetch failed", "category": category}
    rate_per_day = ytd / max(days_into_year, 1)
    extrapolated = ytd + rate_per_day * days_remaining
    out = {
        "source": "NASA EONET v3 + YTD extrapolation",
        "category": category,
        "year": year,
        "as_of": today.isoformat(),
        "ytd_count": ytd,
        "days_into_year": days_into_year,
        "days_remaining": days_remaining,
        "rate_per_day": round(rate_per_day, 4),
        "projected_year_end_count": int(round(extrapolated)),
        "lambda_remaining": round(rate_per_day * days_remaining, 2),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def p_year_end_at_least(proj: dict, threshold_count: int) -> Optional[float]:
    """P(year-end count >= threshold) under Poisson(lambda_remaining) for the
    remaining days, conditioned on the YTD count we've already seen."""
    if not proj or proj.get("error"):
        return None
    ytd = proj.get("ytd_count")
    lam = proj.get("lambda_remaining")
    if ytd is None or lam is None:
        return None
    needed = threshold_count - ytd
    if needed <= 0:
        return 1.0
    total = 0.0
    fact = 1.0
    pwr = 1.0
    for k in range(needed):
        if k > 0:
            fact *= k
            pwr *= lam
        total += math.exp(-lam) * pwr / fact
    return max(0.0, min(1.0, 1.0 - total))


if __name__ == "__main__":
    import json
    print(json.dumps(open_events(category="all"), indent=2)[:1000])
    print(json.dumps(year_end_count_projection(category="wildfires"), indent=2))
