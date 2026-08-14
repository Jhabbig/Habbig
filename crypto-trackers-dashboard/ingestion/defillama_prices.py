"""DefiLlama coins-price API for cross-DEX prices on any chain.

DefiLlama aggregates spot prices across every DEX it tracks (Uniswap V2/V3,
Curve, Aerodrome, Sushiswap, etc.). Free, no key.

Endpoint: https://coins.llama.fi/prices/current/{ids}
  where ids is a comma-separated list of chain:address pairs, e.g.
  "ethereum:0x...,solana:..."

We pre-bake a curated list of "interesting" tokens to track (WBTC, WETH,
USDC across major chains, top L1 wrapped tokens, stablecoins, plus a
handful of liquid mid-caps).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

LLAMA_COINS_BASE = "https://coins.llama.fi"

# Curated tracker list. Each entry: (display, chain:address)
TRACKED_TOKENS = [
    ("WBTC",  "ethereum:0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"),
    ("WETH",  "ethereum:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"),
    ("USDC",  "ethereum:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
    ("USDT",  "ethereum:0xdac17f958d2ee523a2206206994597c13d831ec7"),
    ("DAI",   "ethereum:0x6b175474e89094c44da98b954eedeac495271d0f"),
    ("UNI",   "ethereum:0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"),
    ("LINK",  "ethereum:0x514910771af9ca656af840dff83e8264ecf986ca"),
    ("AAVE",  "ethereum:0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9"),
    ("MKR",   "ethereum:0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2"),
    ("LDO",   "ethereum:0x5a98fcbea516cf06857215779fd812ca3bef1b32"),
    # Solana
    ("SOL",   "solana:So11111111111111111111111111111111111111112"),
    ("JUP",   "solana:JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"),
    ("WIF",   "solana:EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"),
    # Base
    ("AERO",  "base:0x940181a94a35a4569e4529a3cdfb74e38fd98631"),
    # BSC
    ("WBNB",  "bsc:0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"),
    # Arbitrum
    ("ARB",   "arbitrum:0x912ce59144191c1204e64559fe8253a0e49e6548"),
]


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cross_dex_prices() -> dict:
    hit = _cache.get("llama_cross_dex", ttl_s=60)
    if hit is not None:
        return hit
    ids = ",".join(addr for _, addr in TRACKED_TOKENS)
    r = http_get(f"{LLAMA_COINS_BASE}/prices/current/{ids}",
                 timeout=15)
    if not r:
        return {"error": "DefiLlama coins fetch failed", "tokens": []}
    try:
        j = r.json()
    except ValueError:
        return {"error": "DefiLlama coins parse failed", "tokens": []}
    coins = j.get("coins") or {}
    rows: list[dict] = []
    for display, addr in TRACKED_TOKENS:
        d = coins.get(addr) or {}
        rows.append({
            "symbol": display,
            "address": addr,
            "chain": addr.split(":")[0] if ":" in addr else None,
            "price_usd": _f(d.get("price")),
            "confidence": _f(d.get("confidence")),
            "decimals": d.get("decimals"),
            "ts": d.get("timestamp"),
        })
    out = {
        "source": "DefiLlama /prices/current",
        "count": sum(1 for r in rows if r.get("price_usd") is not None),
        "tokens": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("llama_cross_dex", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(cross_dex_prices(), indent=2)[:1500])
