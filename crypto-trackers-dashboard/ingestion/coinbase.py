"""Coinbase Exchange (formerly Pro) public endpoints.

  - /products              - every tradable pair
  - /products/{id}/ticker  - current price + bid/ask
  - /products/{id}/stats   - 24h volume + open/high/low/last
  - /products/{id}/book?level=2 - L2 orderbook

We pull a curated subset of USD pairs for cross-exchange comparison.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

CB_BASE = "https://api.exchange.coinbase.com"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def products() -> dict:
    hit = _cache.get("coinbase_products", ttl_s=3600)
    if hit is not None:
        return hit
    r = http_get(f"{CB_BASE}/products", timeout=20)
    if not r:
        return {"error": "Coinbase /products failed", "products": []}
    try:
        rows = r.json()
    except ValueError:
        return {"error": "Coinbase /products parse failed", "products": []}
    norm = [{
        "id": p.get("id"),
        "base": p.get("base_currency"),
        "quote": p.get("quote_currency"),
        "status": p.get("status"),
    } for p in rows if isinstance(p, dict)]
    out = {
        "source": "Coinbase /products",
        "count": len(norm),
        "products": norm,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("coinbase_products", out)
    return out


def ticker(product_id: str) -> dict:
    pid = product_id.upper()
    hit = _cache.get(f"coinbase_tick_{pid}", ttl_s=10)
    if hit is not None:
        return hit
    r = http_get(f"{CB_BASE}/products/{pid}/ticker", timeout=10)
    if not r:
        return {"error": "Coinbase ticker failed", "product_id": pid}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Coinbase ticker parse failed", "product_id": pid}
    out = {
        "source": "Coinbase /products/{id}/ticker",
        "product_id": pid,
        "price": _f(d.get("price")),
        "bid": _f(d.get("bid")),
        "ask": _f(d.get("ask")),
        "volume_24h": _f(d.get("volume")),
        "time": d.get("time"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"coinbase_tick_{pid}", out)
    return out


def stats(product_id: str) -> dict:
    pid = product_id.upper()
    hit = _cache.get(f"coinbase_stats_{pid}", ttl_s=60)
    if hit is not None:
        return hit
    r = http_get(f"{CB_BASE}/products/{pid}/stats", timeout=10)
    if not r:
        return {"error": "Coinbase stats failed", "product_id": pid}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Coinbase stats parse failed", "product_id": pid}
    out = {
        "source": "Coinbase /products/{id}/stats",
        "product_id": pid,
        "open": _f(d.get("open")),
        "high": _f(d.get("high")),
        "low": _f(d.get("low")),
        "last": _f(d.get("last")),
        "volume_24h": _f(d.get("volume")),
        "volume_30d": _f(d.get("volume_30day")),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"coinbase_stats_{pid}", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(ticker("BTC-USD"), indent=2))
