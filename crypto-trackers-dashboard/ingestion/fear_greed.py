"""Alternative.me Crypto Fear & Greed Index.

Free, no key. Returns daily values 0 (Extreme Fear) - 100 (Extreme Greed)
across the last N days. Updated daily.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

FG_URL = "https://api.alternative.me/fng/"


def index(days: int = 30) -> dict:
    days = max(1, min(days, 365))
    cache_key = f"fng_{days}"
    hit = _cache.get(cache_key, ttl_s=3600)
    if hit is not None:
        return hit
    r = http_get(FG_URL, params={"limit": str(days), "format": "json"}, timeout=15)
    if not r:
        return {"error": "Fear & Greed fetch failed", "rows": []}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Fear & Greed parse failed", "rows": []}
    rows = d.get("data") or []
    norm = []
    for x in rows:
        try:
            ts = int(x.get("timestamp"))
            val = int(x.get("value"))
        except (TypeError, ValueError):
            continue
        norm.append({
            "ts_ms": ts * 1000,
            "date": datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat(),
            "value": val,
            "classification": x.get("value_classification"),
        })
    latest = norm[0] if norm else None
    out = {
        "source": "Alternative.me Crypto Fear & Greed Index",
        "latest": latest,
        "rows": norm,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(index(7), indent=2))
