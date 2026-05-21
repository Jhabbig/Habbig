"""DefiLlama yields API - every DeFi pool with APY + TVL.

  GET https://yields.llama.fi/pools

Free, no auth. Returns ~10k pool entries with apy, apyBase, apyReward,
tvlUsd, chain, project, symbol, exposure (single/multi), il_risk,
stablecoin, ilRisk, mu, sigma.

We filter to pools with tvl >= $1M and sort by APY. Optional category
filter pulls (e.g. "stablecoin", "lending", "yield-aggregator").
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

YIELDS_URL = "https://yields.llama.fi/pools"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def top_yields(min_tvl_usd: float = 1_000_000, limit: int = 100,
               stablecoin_only: bool = False,
               max_il_risk: Optional[str] = None) -> dict:
    """Top DeFi yields by APY, filtered by TVL + optional category."""
    cache_key = f"llama_yields_{min_tvl_usd}_{limit}_{stablecoin_only}_{max_il_risk}"
    hit = _cache.get(cache_key, ttl_s=900)
    if hit is not None:
        return hit
    r = http_get(YIELDS_URL, timeout=30)
    if not r:
        return {"error": "DefiLlama yields fetch failed", "pools": []}
    try:
        d = r.json()
    except ValueError:
        return {"error": "DefiLlama yields parse failed", "pools": []}
    rows_in = d.get("data") or []
    rows: list[dict] = []
    for p in rows_in:
        if not isinstance(p, dict):
            continue
        tvl = _f(p.get("tvlUsd")) or 0
        if tvl < min_tvl_usd:
            continue
        if stablecoin_only and not p.get("stablecoin"):
            continue
        il_risk = p.get("ilRisk") or ""
        if max_il_risk == "no" and il_risk == "yes":
            continue
        apy = _f(p.get("apy"))
        if apy is None or apy <= 0:
            continue
        rows.append({
            "project": p.get("project"),
            "chain": p.get("chain"),
            "symbol": p.get("symbol"),
            "pool_id": p.get("pool"),
            "apy_pct": round(apy, 2),
            "apy_base_pct": _f(p.get("apyBase")),
            "apy_reward_pct": _f(p.get("apyReward")),
            "tvl_usd": tvl,
            "stablecoin": bool(p.get("stablecoin")),
            "il_risk": il_risk,
            "exposure": p.get("exposure"),
            "mu_30d": _f(p.get("mu")),
            "sigma_30d": _f(p.get("sigma")),
            "url": p.get("url"),
        })
    rows.sort(key=lambda p: p["apy_pct"], reverse=True)
    rows = rows[:limit]
    by_project: dict[str, dict] = {}
    by_chain: dict[str, dict] = {}
    for p in rows:
        bp = by_project.setdefault(p["project"], {"project": p["project"], "pools": 0, "tvl_usd": 0})
        bp["pools"] += 1
        bp["tvl_usd"] += p["tvl_usd"]
        bc = by_chain.setdefault(p["chain"] or "?", {"chain": p["chain"], "pools": 0, "tvl_usd": 0})
        bc["pools"] += 1
        bc["tvl_usd"] += p["tvl_usd"]
    out = {
        "source": "DefiLlama /pools (yields.llama.fi)",
        "min_tvl_usd": min_tvl_usd,
        "stablecoin_only": stablecoin_only,
        "max_il_risk": max_il_risk,
        "count": len(rows),
        "pools": rows,
        "by_project": sorted(by_project.values(), key=lambda x: x["tvl_usd"], reverse=True)[:10],
        "by_chain":   sorted(by_chain.values(),   key=lambda x: x["tvl_usd"], reverse=True)[:10],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(top_yields(min_tvl_usd=10_000_000, limit=20,
                                  stablecoin_only=True), indent=2)[:2000])
