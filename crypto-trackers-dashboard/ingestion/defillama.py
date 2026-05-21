"""DefiLlama TVL aggregator (free, no API key).

  - /protocols                   - every DeFi protocol with TVL + chain + category
  - /chains                      - per-chain TVL totals
  - /v2/historicalChainTvl       - TVL time-series per chain
  - /dexs/overview               - DEX volume summary
  - /stablecoins                 - market caps + chain breakdowns
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

LLAMA_BASE = "https://api.llama.fi"
LLAMA_DEFI_BASE = "https://api.llama.fi"
LLAMA_STABLECOIN_BASE = "https://stablecoins.llama.fi"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def chains() -> dict:
    """Per-chain TVL totals + 24h change."""
    hit = _cache.get("llama_chains", ttl_s=900)  # 15 min
    if hit is not None:
        return hit
    r = http_get(f"{LLAMA_BASE}/v2/chains", timeout=20)
    if not r:
        return {"error": "DefiLlama chains failed", "chains": []}
    try:
        rows = r.json()
    except ValueError:
        return {"error": "DefiLlama chains parse failed", "chains": []}
    norm = []
    for c in rows:
        if not isinstance(c, dict):
            continue
        norm.append({
            "name": c.get("name"),
            "chain_id": c.get("chainId"),
            "tvl_usd": _f(c.get("tvl")),
            "token_symbol": c.get("tokenSymbol"),
            "gecko_id": c.get("gecko_id"),
        })
    norm.sort(key=lambda x: x.get("tvl_usd") or 0, reverse=True)
    out = {
        "source": "DefiLlama /v2/chains",
        "count": len(norm),
        "chains": norm,
        "total_tvl_usd": sum(c.get("tvl_usd") or 0 for c in norm),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("llama_chains", out)
    return out


def protocols(limit: int = 100) -> dict:
    """Top-N DeFi protocols by TVL."""
    limit = max(10, min(limit, 500))
    cache_key = f"llama_protocols_{limit}"
    hit = _cache.get(cache_key, ttl_s=900)
    if hit is not None:
        return hit
    r = http_get(f"{LLAMA_BASE}/protocols", timeout=30)
    if not r:
        return {"error": "DefiLlama protocols failed", "protocols": []}
    try:
        rows = r.json()
    except ValueError:
        return {"error": "DefiLlama protocols parse failed", "protocols": []}
    norm = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        norm.append({
            "name": p.get("name"),
            "symbol": (p.get("symbol") or "").upper(),
            "category": p.get("category"),
            "chains": p.get("chains") or [],
            "tvl_usd": _f(p.get("tvl")),
            "change_1d_pct": _f(p.get("change_1d")),
            "change_7d_pct": _f(p.get("change_7d")),
            "change_1m_pct": _f(p.get("change_1m")),
            "mcap": _f(p.get("mcap")),
            "url": p.get("url"),
        })
    norm.sort(key=lambda x: x.get("tvl_usd") or 0, reverse=True)
    norm = norm[:limit]
    out = {
        "source": "DefiLlama /protocols",
        "count": len(norm),
        "protocols": norm,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def stablecoins() -> dict:
    """Top stablecoins by circulating market cap."""
    hit = _cache.get("llama_stables", ttl_s=1800)
    if hit is not None:
        return hit
    r = http_get(f"{LLAMA_STABLECOIN_BASE}/stablecoins",
                 params={"includePrices": "true"}, timeout=20)
    if not r:
        return {"error": "DefiLlama stablecoins failed", "stablecoins": []}
    try:
        d = r.json()
    except ValueError:
        return {"error": "DefiLlama stablecoins parse failed", "stablecoins": []}
    rows = d.get("peggedAssets") or []
    norm = []
    for s in rows:
        if not isinstance(s, dict):
            continue
        circ = (s.get("circulating") or {})
        circ_usd = _f(circ.get("peggedUSD")) if isinstance(circ, dict) else None
        norm.append({
            "name": s.get("name"),
            "symbol": (s.get("symbol") or "").upper(),
            "peg_type": s.get("pegType"),
            "peg_mechanism": s.get("pegMechanism"),
            "price": _f(s.get("price")),
            "circulating_usd": circ_usd,
            "chains": s.get("chains") or [],
        })
    norm.sort(key=lambda x: x.get("circulating_usd") or 0, reverse=True)
    out = {
        "source": "DefiLlama /stablecoins",
        "count": len(norm),
        "stablecoins": norm[:50],
        "total_circulating_usd": sum(s.get("circulating_usd") or 0 for s in norm),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("llama_stables", out)
    return out


def dex_overview() -> dict:
    """24h DEX volume across all chains."""
    hit = _cache.get("llama_dexs", ttl_s=900)
    if hit is not None:
        return hit
    r = http_get(f"{LLAMA_BASE}/overview/dexs", timeout=20)
    if not r:
        return {"error": "DefiLlama DEX overview failed"}
    try:
        d = r.json()
    except ValueError:
        return {"error": "DefiLlama DEX parse failed"}
    proto = d.get("protocols") or []
    top = []
    for p in proto[:30]:
        if not isinstance(p, dict):
            continue
        top.append({
            "name": p.get("name"),
            "category": p.get("category"),
            "chains": p.get("chains") or [],
            "volume_24h_usd": _f(p.get("total24h")),
            "volume_7d_usd": _f(p.get("total7d")),
            "change_1d_pct": _f(p.get("change_1d")),
            "change_7d_pct": _f(p.get("change_7d")),
        })
    out = {
        "source": "DefiLlama /overview/dexs",
        "total_24h_usd": _f(d.get("total24h")),
        "total_7d_usd": _f(d.get("total7d")),
        "change_1d_pct": _f(d.get("change_1d")),
        "change_7d_pct": _f(d.get("change_7d")),
        "top_protocols": top,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("llama_dexs", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(chains(), indent=2)[:1500])
    print(json.dumps(dex_overview(), indent=2)[:1500])
