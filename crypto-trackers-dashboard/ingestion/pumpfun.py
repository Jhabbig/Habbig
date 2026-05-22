"""Pump.fun memecoin trending feed.

Pump.fun is the dominant Solana memecoin launchpad. They expose a public
frontend API at https://frontend-api.pump.fun for coin discovery:

  GET /coins?sort=market_cap&order=DESC&limit=50
  GET /coins/trending

No auth. Rate-limited; cache 60s.

We surface the trending list with mcap, USD reserves, dev wallet,
graduation status (whether the token has migrated from the bonding
curve to Raydium liquidity), and any social links.

For dashboard users this is a "what's hot on Solana right now" signal —
useful for memecoin traders and for spotting unusual launch volume
that often precedes mainstream attention.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

BASE = "https://frontend-api.pump.fun"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def trending(limit: int = 30) -> dict:
    limit = max(5, min(limit, 100))
    cache_key = f"pumpfun_{limit}"
    hit = _cache.get(cache_key, ttl_s=60)
    if hit is not None:
        return hit
    r = http_get(f"{BASE}/coins",
                 params={"sort": "market_cap", "order": "DESC",
                         "limit": str(limit), "offset": "0"},
                 timeout=15)
    if not r:
        return {"error": "Pump.fun fetch failed", "coins": []}
    try:
        rows_in = r.json()
    except ValueError:
        return {"error": "Pump.fun parse failed", "coins": []}
    rows: list[dict] = []
    for c in rows_in:
        if not isinstance(c, dict):
            continue
        mcap = _f(c.get("usd_market_cap"))
        rows.append({
            "name": c.get("name"),
            "symbol": (c.get("symbol") or "").upper(),
            "mint": c.get("mint"),
            "description": (c.get("description") or "")[:140],
            "image_uri": c.get("image_uri"),
            "market_cap_usd": mcap,
            "virtual_sol_reserves": _f(c.get("virtual_sol_reserves")),
            "virtual_token_reserves": _f(c.get("virtual_token_reserves")),
            "creator": c.get("creator"),
            "twitter": c.get("twitter"),
            "telegram": c.get("telegram"),
            "website": c.get("website"),
            "complete": bool(c.get("complete")),  # has graduated to Raydium
            "created_timestamp": c.get("created_timestamp"),
            "raydium_pool": c.get("raydium_pool"),
        })
    rows.sort(key=lambda r: r.get("market_cap_usd") or 0, reverse=True)
    graduated = sum(1 for r in rows if r.get("complete"))
    out = {
        "source": "Pump.fun frontend-api /coins (Solana memecoin launchpad)",
        "count": len(rows),
        "graduated_to_raydium": graduated,
        "still_on_bonding_curve": len(rows) - graduated,
        "coins": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(trending(20), indent=2)[:2000])
