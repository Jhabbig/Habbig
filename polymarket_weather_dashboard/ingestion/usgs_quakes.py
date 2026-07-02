"""USGS earthquake feed.

Two views:

  * ``recent_quakes(min_magnitude, days)`` - the last N days of M{min}+ quakes
    globally, used for the live Activity panel.
  * ``year_end_projection(min_magnitude)`` - YTD count + linear extrapolation
    to year-end + Poisson-derived probability that the year ends >= each
    integer count, plus the historical 30-year mean for context.

USGS FDSN endpoint is free, no key. Hard-cap at 5000 results per query (USGS
limit is 20000 but smaller responses keep the dashboard snappy).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

FDSN_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# 30-year (1990-2019) USGS climatology counts at mag thresholds. Used as the
# Poisson lambda when projecting a year-end count under "no remaining trend".
# Source: USGS Earthquake Catalog historical averages.
ANNUAL_CLIMO: dict[float, float] = {
    5.0: 1500.0,
    5.5: 500.0,
    6.0: 142.0,
    6.5: 49.0,
    7.0: 15.0,
    7.5: 4.0,
    8.0: 1.0,
}


def _round_thresh(mag: float) -> float:
    """Round to the nearest threshold we have a climo for."""
    keys = sorted(ANNUAL_CLIMO.keys())
    return min(keys, key=lambda k: abs(k - mag))


def _query(starttime: str, endtime: str, min_mag: float, limit: int = 5000) -> Optional[list[dict]]:
    r = http_get(FDSN_URL, params={
        "format": "geojson",
        "starttime": starttime,
        "endtime": endtime,
        "minmagnitude": f"{min_mag:.2f}",
        "orderby": "magnitude",
        "limit": str(limit),
    }, timeout=30)
    if not r:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    out: list[dict] = []
    for f in data.get("features", []):
        props = f.get("properties", {}) or {}
        geom = f.get("geometry", {}) or {}
        coords = geom.get("coordinates", []) or []
        mag = props.get("mag")
        if mag is None:
            continue
        out.append({
            "id": f.get("id"),
            "mag": round(float(mag), 2),
            "place": props.get("place") or "",
            "time_ms": int(props.get("time") or 0),
            "time_iso": datetime.fromtimestamp((props.get("time") or 0) / 1000, tz=timezone.utc).isoformat(),
            "url": props.get("url"),
            "tsunami": int(props.get("tsunami") or 0),
            "lat": coords[1] if len(coords) >= 2 else None,
            "lon": coords[0] if len(coords) >= 1 else None,
            "depth_km": coords[2] if len(coords) >= 3 else None,
        })
    return out


def recent_quakes(min_magnitude: float = 5.0, days: int = 30) -> dict:
    cache_key = f"quakes_recent_{min_magnitude:.1f}_{days}"
    hit = _cache.get(cache_key, ttl_s=300)  # 5 min
    if hit is not None:
        return hit
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rows = _query(start.strftime("%Y-%m-%dT%H:%M:%S"),
                  end.strftime("%Y-%m-%dT%H:%M:%S"),
                  min_mag=min_magnitude)
    if rows is None:
        return {"error": "USGS fetch failed", "quakes": [], "count": 0}
    biggest = max(rows, key=lambda q: q["mag"]) if rows else None
    out = {
        "source": "USGS FDSN event/1 query",
        "min_magnitude": min_magnitude,
        "window_days": days,
        "count": len(rows),
        "biggest": biggest,
        "quakes": rows[:200],  # cap on the wire
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def _ytd_count(min_mag: float, year: int) -> Optional[int]:
    start = date(year, 1, 1).strftime("%Y-%m-%dT00:00:00")
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # USGS rejects the count endpoint for very small responses with HTTP 400 in
    # some configurations - safer to use the regular query but ask for the
    # smallest payload.
    r = http_get(FDSN_URL, params={
        "format": "geojson",
        "starttime": start,
        "endtime": end,
        "minmagnitude": f"{min_mag:.2f}",
        "limit": "20000",
    }, timeout=30)
    if not r:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return len(data.get("features", []) or [])


def year_end_projection(min_magnitude: float = 5.0) -> dict:
    cache_key = f"quake_projection_{min_magnitude:.1f}"
    hit = _cache.get(cache_key, ttl_s=900)  # 15 min
    if hit is not None:
        return hit
    today = datetime.now(timezone.utc).date()
    year = today.year
    days_into_year = (today - date(year, 1, 1)).days + 1
    days_in_year = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    days_remaining = days_in_year - days_into_year
    ytd = _ytd_count(min_magnitude, year)
    if ytd is None:
        return {"error": "USGS fetch failed", "min_magnitude": min_magnitude}
    rate_per_day = ytd / max(days_into_year, 1)
    extrapolated = ytd + rate_per_day * days_remaining
    # Climatological annual rate for this threshold
    thresh = _round_thresh(min_magnitude)
    climo_annual = ANNUAL_CLIMO.get(thresh)
    # Poisson lambda for the *remaining* days, using the YTD-implied rate
    # (pre-conditioned on what we've already seen this year).
    lam_remaining = rate_per_day * days_remaining
    out = {
        "source": "USGS FDSN + climatological priors",
        "year": year,
        "as_of": today.isoformat(),
        "min_magnitude": min_magnitude,
        "ytd_count": ytd,
        "days_into_year": days_into_year,
        "days_remaining": days_remaining,
        "rate_per_day": round(rate_per_day, 4),
        "projected_year_end_count": int(round(extrapolated)),
        "climatological_annual": climo_annual,
        "lambda_remaining": round(lam_remaining, 2),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def p_year_end_at_least(proj: dict, threshold_count: int) -> Optional[float]:
    """Probability the year ends with >= ``threshold_count`` events, given the
    YTD count and a Poisson model for the remainder of the year."""
    if not proj or proj.get("error"):
        return None
    ytd = proj.get("ytd_count")
    lam = proj.get("lambda_remaining")
    if ytd is None or lam is None:
        return None
    needed = threshold_count - ytd
    if needed <= 0:
        return 1.0
    # P(N >= needed) where N ~ Poisson(lam) = 1 - sum_{k=0..needed-1} e^-lam lam^k / k!
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
    # Smoke test:  python3 -m ingestion.usgs_quakes
    import json
    print(json.dumps(recent_quakes(min_magnitude=6.0, days=30), indent=2)[:800])
    print(json.dumps(year_end_projection(min_magnitude=6.0), indent=2))
