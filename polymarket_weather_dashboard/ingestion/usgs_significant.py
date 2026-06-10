"""USGS significant-earthquakes feed (PAGER impact).

The USGS publishes a curated "significant" feed - quakes with notable
magnitude, felt reports, or PAGER impact. PAGER (Prompt Assessment of
Global Earthquakes for Response) gives expected fatality/economic loss
distributions per event.

Endpoint:
    https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson

Returns up to ~30 days of quakes with the alert level: GREEN / YELLOW /
ORANGE / RED based on PAGER probability bins for fatalities.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

SIG_URL_MONTH = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson"
SIG_URL_WEEK = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson"

ALERT_RANK = {"red": 0, "orange": 1, "yellow": 2, "green": 3}


def _summarise(f: dict) -> dict:
    props = f.get("properties") or {}
    geom = f.get("geometry") or {}
    coords = geom.get("coordinates") or []
    return {
        "id": f.get("id"),
        "mag": props.get("mag"),
        "place": props.get("place"),
        "time_iso": datetime.fromtimestamp((props.get("time") or 0) / 1000,
                                              tz=timezone.utc).isoformat(),
        "alert": props.get("alert"),
        "tsunami": int(props.get("tsunami") or 0),
        "sig": props.get("sig"),
        "url": props.get("url"),
        "felt": props.get("felt"),
        "cdi": props.get("cdi"),  # community decimal intensity
        "mmi": props.get("mmi"),  # modified Mercalli intensity
        "lon": coords[0] if len(coords) >= 1 else None,
        "lat": coords[1] if len(coords) >= 2 else None,
        "depth_km": coords[2] if len(coords) >= 3 else None,
    }


def significant_recent(window: str = "month") -> dict:
    """``window`` is "week" or "month"."""
    url = SIG_URL_WEEK if window == "week" else SIG_URL_MONTH
    cache_key = f"usgs_sig_{window}"
    hit = _cache.get(cache_key, ttl_s=600)
    if hit is not None:
        return hit
    r = http_get(url, timeout=20)
    if not r:
        return {"error": "USGS significant fetch failed", "events": [], "count": 0}
    try:
        data = r.json()
    except ValueError:
        return {"error": "USGS significant parse failed", "events": [], "count": 0}
    feats = data.get("features") or []
    rows = [_summarise(f) for f in feats]
    rows.sort(key=lambda r: (ALERT_RANK.get((r.get("alert") or "").lower(), 99),
                                  -(r.get("mag") or 0)))
    by_alert: dict[str, int] = {}
    for r in rows:
        a = r.get("alert") or "none"
        by_alert[a] = by_alert.get(a, 0) + 1
    out = {
        "source": f"USGS significant_{window}.geojson",
        "window": window,
        "count": len(rows),
        "events": rows,
        "by_alert": by_alert,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(significant_recent("month"), indent=2)[:1500])
