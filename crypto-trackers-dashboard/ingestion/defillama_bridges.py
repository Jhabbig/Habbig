"""DefiLlama bridges API - cross-chain bridge volumes + TVL.

  GET https://bridges.llama.fi/bridges
  GET https://bridges.llama.fi/bridgevolume/{bridge_id}

Free, no auth. We pull the bridges list (Wormhole, Stargate, Across,
deBridge, LayerZero, Synapse, cBridge, etc.) and surface their 24h /
7d volumes + total TVL secured.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

BRIDGES_URL = "https://bridges.llama.fi/bridges"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def overview() -> dict:
    hit = _cache.get("llama_bridges", ttl_s=900)
    if hit is not None:
        return hit
    r = http_get(BRIDGES_URL, params={"includeChains": "true"}, timeout=20)
    if not r:
        return {"error": "DefiLlama bridges fetch failed", "bridges": []}
    try:
        d = r.json()
    except ValueError:
        return {"error": "DefiLlama bridges parse failed", "bridges": []}
    rows_in = d.get("bridges") or []
    rows: list[dict] = []
    for b in rows_in:
        if not isinstance(b, dict):
            continue
        rows.append({
            "id": b.get("id"),
            "name": b.get("displayName") or b.get("name"),
            "chains": b.get("chains") or [],
            "destination_chain": b.get("destinationChain"),
            "volume_24h_usd": _f(b.get("lastDailyVolume")),
            "volume_7d_usd": _f(b.get("weeklyVolume")),
            "monthly_volume_usd": _f(b.get("monthlyVolume")),
            "tx_count_24h": _f(b.get("dailyTxs")),
            "tx_count_7d": _f(b.get("weeklyTxs")),
            "icon": b.get("icon"),
        })
    rows.sort(key=lambda r: r.get("volume_24h_usd") or 0, reverse=True)
    out = {
        "source": "DefiLlama bridges /bridges",
        "count": len(rows),
        "bridges": rows[:30],
        "total_volume_24h_usd": sum(r.get("volume_24h_usd") or 0 for r in rows),
        "total_volume_7d_usd": sum(r.get("volume_7d_usd") or 0 for r in rows),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("llama_bridges", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(overview(), indent=2)[:1500])
