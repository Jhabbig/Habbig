"""Top NFT collection floors via Reservoir public API.

Reservoir aggregates floors across OpenSea, Blur, LooksRare, X2Y2, etc.
Their /collections/v7 endpoint returns the top N by 1-day volume with
floor + sales + market cap. Public; rate-limited without an API key.

We pull the top 30 by 1-day volume on Ethereum mainnet.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

# Reservoir API host (free public tier)
RESERVOIR_HOST = "https://api.reservoir.tools"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _headers() -> dict:
    key = os.environ.get("RESERVOIR_API_KEY")
    return {"x-api-key": key} if key else {}


def top_collections(limit: int = 30) -> dict:
    """Top N NFT collections by 1-day volume on Ethereum mainnet."""
    limit = max(5, min(limit, 50))
    cache_key = f"reservoir_top_{limit}"
    hit = _cache.get(cache_key, ttl_s=600)
    if hit is not None:
        return hit
    r = http_get(f"{RESERVOIR_HOST}/collections/v7",
                 params={"sortBy": "1DayVolume", "limit": str(limit)},
                 timeout=15, headers=_headers())
    if not r:
        return {"error": "Reservoir fetch failed", "collections": [],
                "note": "Set RESERVOIR_API_KEY for higher rate limits."}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Reservoir parse failed", "collections": []}
    rows_in = d.get("collections") or []
    rows: list[dict] = []
    for c in rows_in:
        if not isinstance(c, dict):
            continue
        floor = c.get("floorAsk") or {}
        floor_price = (floor.get("price") or {}).get("amount") or {}
        floor_eth = _f(floor_price.get("decimal"))
        floor_usd = _f(floor_price.get("usd"))
        vol = c.get("volume") or {}
        rows.append({
            "name": c.get("name"),
            "symbol": c.get("symbol"),
            "image": c.get("image"),
            "contract": c.get("primaryContract"),
            "supply": _f(c.get("tokenCount")),
            "owners": _f(c.get("ownerCount")),
            "floor_eth": floor_eth,
            "floor_usd": floor_usd,
            "volume_1d_eth":  _f(vol.get("1day")),
            "volume_7d_eth":  _f(vol.get("7day")),
            "volume_30d_eth": _f(vol.get("30day")),
            "all_time_volume_eth": _f(vol.get("allTime")),
            "marketcap_usd": _f(c.get("marketCap")),
            "external_url": (c.get("externalUrl")
                              or f"https://opensea.io/collection/{c.get('slug')}"
                              if c.get("slug") else None),
        })
    out = {
        "source": "Reservoir /collections/v7",
        "chain": "ethereum",
        "count": len(rows),
        "collections": rows,
        "total_volume_1d_eth": sum(r.get("volume_1d_eth") or 0 for r in rows),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(top_collections(20), indent=2)[:2000])
