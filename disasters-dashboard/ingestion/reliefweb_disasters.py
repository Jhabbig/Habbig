"""ReliefWeb disaster API - humanitarian impact view.

ReliefWeb (run by OCHA) maintains a catalog of every reportable disaster
since 1980 with humanitarian impact metadata: country, date, type, status
(alert / ongoing / past), affected/dead/injured estimates where available,
and links to situation reports.

API:  https://api.reliefweb.int/v1/disasters
No auth, no key.

We pull "ongoing" and "alert" disasters (current humanitarian crises)
sorted by date desc.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import _cache
from ._http import get as http_get

RELIEFWEB_URL = "https://api.reliefweb.int/v1/disasters"


def _summarise(item: dict) -> dict:
    fields = item.get("fields") or {}
    countries = [c.get("name") for c in (fields.get("country") or []) if isinstance(c, dict)]
    types = [t.get("name") for t in (fields.get("type") or []) if isinstance(t, dict)]
    status = (fields.get("status") or "")
    glide = fields.get("glide") or ""
    return {
        "id": item.get("id"),
        "name": fields.get("name") or "",
        "status": status,
        "glide": glide,
        "countries": countries,
        "types": types,
        "date": (fields.get("date") or {}).get("created"),
        "url": fields.get("url"),
    }


def ongoing_disasters(limit: int = 60) -> dict:
    hit = _cache.get(f"reliefweb_{limit}", ttl_s=3600)
    if hit is not None:
        return hit
    params = {
        "appname": "narve-disasters-dashboard",
        "profile": "list",
        "preset": "latest",
        "limit": str(limit),
        "filter[field]": "status",
        "filter[value][]": "ongoing",
    }
    r = http_get(RELIEFWEB_URL, params=params, timeout=20)
    if not r:
        return {"error": "ReliefWeb fetch failed", "disasters": [], "count": 0}
    try:
        data = r.json()
    except ValueError:
        return {"error": "ReliefWeb parse failed", "disasters": [], "count": 0}
    rows = [_summarise(item) for item in (data.get("data") or [])]
    by_type: dict[str, int] = {}
    by_country: dict[str, int] = {}
    for r_ in rows:
        for t in r_.get("types") or []:
            by_type[t] = by_type.get(t, 0) + 1
        for c in r_.get("countries") or []:
            by_country[c] = by_country.get(c, 0) + 1
    out = {
        "source": "ReliefWeb v1 disasters API",
        "count": len(rows),
        "disasters": rows,
        "by_type": by_type,
        "top_countries": dict(sorted(by_country.items(), key=lambda kv: -kv[1])[:10]),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"reliefweb_{limit}", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(ongoing_disasters(limit=30), indent=2)[:2500])
