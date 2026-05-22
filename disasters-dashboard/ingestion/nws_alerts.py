"""US National Weather Service active alerts feed.

api.weather.gov/alerts/active is free, no key, but does require a polite
User-Agent (already set in ``_http.py``). It returns CAP-format JSON of every
currently-active alert in the US: tornado warnings, severe thunderstorm
warnings, flash flood warnings, fire weather watches, etc.

We surface counts and the top-N most-severe items.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import _cache
from ._http import get as http_get

NWS_BASE = "https://api.weather.gov/alerts/active"

SEVERITY_ORDER = ["Extreme", "Severe", "Moderate", "Minor", "Unknown"]


def _summarise(alert: dict) -> dict:
    p = alert.get("properties") or {}
    return {
        "id": alert.get("id") or p.get("id"),
        "event": p.get("event"),
        "severity": p.get("severity"),
        "urgency": p.get("urgency"),
        "certainty": p.get("certainty"),
        "headline": p.get("headline"),
        "area": p.get("areaDesc"),
        "sender": p.get("senderName"),
        "effective": p.get("effective"),
        "expires": p.get("expires"),
        "ends": p.get("ends"),
    }


def active_alerts(severity: str = "Severe") -> dict:
    cache_key = f"nws_active_{severity}"
    hit = _cache.get(cache_key, ttl_s=180)  # 3 min - alerts move fast
    if hit is not None:
        return hit
    params = {"limit": "200"}
    if severity in SEVERITY_ORDER:
        params["severity"] = severity
    r = http_get(NWS_BASE, params=params, timeout=20)
    if not r:
        return {"error": "NWS fetch failed", "alerts": [], "count": 0}
    try:
        data = r.json()
    except ValueError:
        return {"error": "NWS parse failed", "alerts": [], "count": 0}
    features = data.get("features") or []
    summary = [_summarise(f) for f in features]
    # Order by severity then urgency
    sev_rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    summary.sort(key=lambda a: (sev_rank.get(a.get("severity") or "Unknown", 5),
                                  a.get("event") or ""))
    by_event: dict[str, int] = {}
    for s in summary:
        ev = s.get("event") or "Unknown"
        by_event[ev] = by_event.get(ev, 0) + 1
    out = {
        "source": "api.weather.gov/alerts/active",
        "severity_filter": severity,
        "count": len(summary),
        "alerts": summary[:50],
        "by_event": by_event,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(active_alerts(severity="Severe"), indent=2)[:1500])
