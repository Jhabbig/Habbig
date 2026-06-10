"""NWS active flood-related alerts subset.

Layers on top of ``nws_alerts`` to extract just the flood-typed alerts:
Flash Flood Warning / Watch, Flood Warning / Watch / Advisory, Coastal Flood
Warning / Watch / Advisory, Storm Surge Warning / Watch.

We hit the same ``api.weather.gov/alerts/active`` endpoint with a
specifically-typed filter so the response is small and we can show the
flood-specific subset on its own panel.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import _cache
from ._http import get as http_get

NWS_BASE = "https://api.weather.gov/alerts/active"

FLOOD_EVENTS = (
    "Flash Flood Warning", "Flash Flood Watch", "Flash Flood Statement",
    "Flood Warning", "Flood Watch", "Flood Advisory", "Flood Statement",
    "Coastal Flood Warning", "Coastal Flood Watch", "Coastal Flood Advisory",
    "Storm Surge Warning", "Storm Surge Watch",
)


def active_flood_alerts() -> dict:
    hit = _cache.get("nws_floods", ttl_s=180)
    if hit is not None:
        return hit
    # The /alerts/active endpoint accepts a comma-separated `event` filter
    params = {"event": ",".join(FLOOD_EVENTS), "limit": "200"}
    r = http_get(NWS_BASE, params=params, timeout=20)
    if not r:
        return {"error": "NWS flood fetch failed", "alerts": [], "count": 0}
    try:
        data = r.json()
    except ValueError:
        return {"error": "NWS flood parse failed", "alerts": [], "count": 0}
    features = data.get("features") or []
    rows: list[dict] = []
    for f in features:
        p = f.get("properties") or {}
        rows.append({
            "id": f.get("id"),
            "event": p.get("event"),
            "severity": p.get("severity"),
            "urgency": p.get("urgency"),
            "headline": (p.get("headline") or "")[:160],
            "area": p.get("areaDesc"),
            "effective": p.get("effective"),
            "expires": p.get("expires"),
        })
    by_event: dict[str, int] = {}
    flash_count = 0
    surge_count = 0
    for r_ in rows:
        ev = r_.get("event") or "?"
        by_event[ev] = by_event.get(ev, 0) + 1
        if ev.startswith("Flash"):
            flash_count += 1
        if ev.startswith("Storm Surge"):
            surge_count += 1
    out = {
        "source": "api.weather.gov/alerts/active (flood subset)",
        "count": len(rows),
        "flash_flood_count": flash_count,
        "storm_surge_count": surge_count,
        "alerts": rows[:60],
        "by_event": by_event,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("nws_floods", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(active_flood_alerts(), indent=2)[:1500])
