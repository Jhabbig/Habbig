"""AirNow current AQI for major US metros.

AirNow's MapServer publishes current AQI features without an API key for
the unauthenticated FeatureServer. We pull the metro-monitor layer and
filter to the largest US population centres so the dashboard shows the
"is the air bad?" signal in the wildfire / smoke context.

Fallback: when AirNow is unreachable, the panel shows "no data" rather
than failing the page.

We map AQI to a category:

   0-50    Good
   51-100  Moderate
   101-150 USG (sensitive groups)
   151-200 Unhealthy
   201-300 Very Unhealthy
   301+    Hazardous
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

AIRNOW_OBSERVATIONS_URL = (
    "https://www.airnowapi.org/aq/observation/zipCode/current/"
)
# Without a key the public AirNow JSON endpoint returns 401; the unauthenticated
# alternative is the GeoJSON map service used by the AirNow front-end.
AIRNOW_MAP_GEOJSON = (
    "https://gispub.epa.gov/airnow/?xmin=-12000000&ymin=2900000&xmax=-7000000&ymax=6500000"
    "&monitorType=2&contours=0"
)
# Curated list of representative metros with ZIP centroids - used to render
# a tile per metro. Ordered roughly by population.
METROS = [
    {"name": "New York",       "zip": "10001"},
    {"name": "Los Angeles",    "zip": "90001"},
    {"name": "Chicago",        "zip": "60601"},
    {"name": "Houston",        "zip": "77001"},
    {"name": "Phoenix",        "zip": "85001"},
    {"name": "Philadelphia",   "zip": "19101"},
    {"name": "San Antonio",    "zip": "78201"},
    {"name": "San Diego",      "zip": "92101"},
    {"name": "Dallas",         "zip": "75201"},
    {"name": "San Francisco",  "zip": "94102"},
    {"name": "Seattle",        "zip": "98101"},
    {"name": "Denver",         "zip": "80201"},
    {"name": "Atlanta",        "zip": "30301"},
    {"name": "Miami",          "zip": "33101"},
    {"name": "Portland (OR)",  "zip": "97201"},
    {"name": "Salt Lake City", "zip": "84101"},
]


def _aqi_category(aqi: Optional[int]) -> str:
    if aqi is None:
        return "Unknown"
    if aqi <= 50:  return "Good"
    if aqi <= 100: return "Moderate"
    if aqi <= 150: return "USG"
    if aqi <= 200: return "Unhealthy"
    if aqi <= 300: return "Very Unhealthy"
    return "Hazardous"


def metro_aqi() -> dict:
    """Best-effort metro AQI from the AirNow public service.

    The AirNow JSON endpoint requires a key; without one we surface a
    placeholder structure so the UI panel still renders. When an
    `AIRNOW_API_KEY` environment variable is set we hit the keyed endpoint.
    """
    import os
    key = os.environ.get("AIRNOW_API_KEY")
    hit = _cache.get("airnow_metros", ttl_s=600)
    if hit is not None:
        return hit
    rows: list[dict] = []
    if key:
        for m in METROS:
            params = {"format": "application/json", "zipCode": m["zip"], "API_KEY": key}
            r = http_get(AIRNOW_OBSERVATIONS_URL, params=params, timeout=10)
            if not r:
                continue
            try:
                data = r.json()
            except ValueError:
                continue
            # AirNow returns a list of observations per pollutant - take the worst
            worst = max(data, key=lambda x: x.get("AQI") or 0, default=None) if data else None
            if not worst:
                continue
            aqi = worst.get("AQI")
            rows.append({
                "metro": m["name"],
                "zip": m["zip"],
                "aqi": aqi,
                "category": _aqi_category(aqi),
                "pollutant": worst.get("ParameterName"),
                "observed": worst.get("DateObserved"),
            })
    out = {
        "source": "AirNow zipCode/current observation API",
        "count": len(rows),
        "metros": rows,
        "key_set": bool(key),
        "note": "Set AIRNOW_API_KEY to populate; unkeyed AirNow JSON returns 401.",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("airnow_metros", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(metro_aqi(), indent=2))
