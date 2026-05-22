"""DefiLlama fees + revenue API — per-chain and per-protocol.

  /overview/fees?dataType=dailyFees
  /overview/fees?dataType=dailyRevenue

Free, no auth. Returns daily fees aggregated across protocols.

We use this for:
  - L2 sequencer revenue per chain (Arbitrum, Optimism, Base, Blast,
    Linea, ZkSync). These are the L2 networks' revenue from posting
    L1 calldata + sequencer tips, minus L1 batching costs.
  - Per-protocol fee + revenue snapshots.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

BASE = "https://api.llama.fi"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def chain_fees() -> dict:
    """Per-chain fees + revenue 24h."""
    hit = _cache.get("llama_chain_fees", ttl_s=1800)
    if hit is not None:
        return hit
    r = http_get(f"{BASE}/overview/fees", params={"dataType": "dailyFees"},
                 timeout=20)
    if not r:
        return {"error": "DefiLlama fees fetch failed", "chains": []}
    try:
        d = r.json()
    except ValueError:
        return {"error": "DefiLlama fees parse failed", "chains": []}
    # API returns aggregated across all protocols; the chain breakdown sits in
    # the "protocols" entry list with a `chains` field per protocol. We sum by
    # primary chain to surface a per-chain rollup.
    by_chain: dict[str, dict] = {}
    for p in (d.get("protocols") or []):
        if not isinstance(p, dict):
            continue
        chains = p.get("chains") or []
        if not chains:
            continue
        primary = chains[0]
        fee_24h = _f(p.get("total24h")) or 0
        fee_7d = _f(p.get("total7d")) or 0
        c = by_chain.setdefault(primary, {
            "chain": primary, "fees_24h_usd": 0, "fees_7d_usd": 0, "protocols": 0,
        })
        c["fees_24h_usd"] += fee_24h
        c["fees_7d_usd"] += fee_7d
        c["protocols"] += 1
    rows = sorted(by_chain.values(), key=lambda x: x["fees_24h_usd"], reverse=True)
    out = {
        "source": "DefiLlama /overview/fees (daily fees, aggregated by chain)",
        "count": len(rows),
        "chains": rows,
        "total_fees_24h_usd": sum(c["fees_24h_usd"] for c in rows),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("llama_chain_fees", out)
    return out


L2_CHAINS = {"Arbitrum", "Optimism", "Base", "Blast", "Linea", "ZkSync Era",
             "Scroll", "Mantle", "Polygon zkEVM", "Mode", "Manta"}


def l2_sequencer_revenue() -> dict:
    """Filter chain_fees() to known L2 networks."""
    full = chain_fees()
    if full.get("error"):
        return full
    rows = [c for c in (full.get("chains") or []) if c["chain"] in L2_CHAINS]
    rows.sort(key=lambda c: c["fees_24h_usd"], reverse=True)
    return {
        "source": "L2 chains subset of DefiLlama daily fees",
        "count": len(rows),
        "chains": rows,
        "total_l2_fees_24h_usd": sum(c["fees_24h_usd"] for c in rows),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def restaking_protocols() -> dict:
    """Restaking-category protocols (EigenLayer + AVS economy)."""
    hit = _cache.get("llama_restaking", ttl_s=900)
    if hit is not None:
        return hit
    r = http_get(f"{BASE}/protocols", timeout=30)
    if not r:
        return {"error": "DefiLlama protocols fetch failed", "protocols": []}
    try:
        rows_in = r.json()
    except ValueError:
        return {"error": "DefiLlama protocols parse failed", "protocols": []}
    rows: list[dict] = []
    for p in rows_in:
        if not isinstance(p, dict):
            continue
        cat = p.get("category") or ""
        if cat not in ("Restaking", "Liquid Restaking", "Liquid Staking"):
            continue
        rows.append({
            "name": p.get("name"),
            "symbol": (p.get("symbol") or "").upper(),
            "category": cat,
            "chains": p.get("chains") or [],
            "tvl_usd": _f(p.get("tvl")),
            "change_24h_pct": _f(p.get("change_1d")),
            "change_7d_pct": _f(p.get("change_7d")),
            "mcap": _f(p.get("mcap")),
            "url": p.get("url"),
        })
    rows.sort(key=lambda r: r.get("tvl_usd") or 0, reverse=True)
    by_cat: dict[str, dict] = {}
    for r_ in rows:
        b = by_cat.setdefault(r_["category"], {"category": r_["category"],
                                                  "tvl_usd": 0, "protocols": 0})
        b["tvl_usd"] += r_.get("tvl_usd") or 0
        b["protocols"] += 1
    out = {
        "source": "DefiLlama /protocols (Restaking + Liquid Restaking + Liquid Staking)",
        "count": len(rows),
        "protocols": rows[:50],
        "by_category": sorted(by_cat.values(), key=lambda b: b["tvl_usd"],
                              reverse=True),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("llama_restaking", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(l2_sequencer_revenue(), indent=2)[:1500])
    print(json.dumps(restaking_protocols(), indent=2)[:1500])
