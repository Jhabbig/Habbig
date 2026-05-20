"""Solscan public API for Solana SPL token context.

Solscan offers a free public API (no key required for low cadence) at
https://public-api.solscan.io. We hit:
  - /token/meta?tokenAddress=  - holder count, supply, name, icon
  - /token/holders?tokenAddress=&limit=10 - top holders
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

# Public API host - Solscan also has a Pro API at https://pro-api.solscan.io
# requiring a JWT. The public host serves slower rate limits but no key.
PUBLIC_BASE = "https://public-api.solscan.io"


def _headers() -> dict:
    """Solscan accepts an optional 'token' header for higher rate limits."""
    token = os.environ.get("SOLSCAN_API_KEY")
    if token:
        return {"token": token}
    return {}


def token_meta(token_address: str) -> dict:
    """SPL token metadata + holder count."""
    cache_key = f"solscan_meta_{token_address}"
    hit = _cache.get(cache_key, ttl_s=1800)
    if hit is not None:
        return hit
    r = http_get(f"{PUBLIC_BASE}/token/meta",
                 params={"tokenAddress": token_address}, timeout=12,
                 headers=_headers())
    if not r:
        return {"error": "Solscan meta fetch failed", "token": token_address}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Solscan meta parse failed", "token": token_address}

    def _f(v) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    out = {
        "source": "Solscan /token/meta",
        "token_address": token_address,
        "name": d.get("name") or d.get("symbol"),
        "symbol": d.get("symbol"),
        "decimals": d.get("decimals"),
        "icon": d.get("icon"),
        "supply": _f(d.get("supply")),
        "holder_count": _f(d.get("holder")),
        "market_cap_usd": _f(d.get("marketCapFD")),
        "twitter": d.get("twitter"),
        "website": d.get("website"),
        "tag": d.get("tag"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def top_holders(token_address: str, limit: int = 10) -> dict:
    """Top N SPL holders for a token."""
    limit = max(1, min(limit, 20))
    cache_key = f"solscan_holders_{token_address}_{limit}"
    hit = _cache.get(cache_key, ttl_s=3600)
    if hit is not None:
        return hit
    r = http_get(f"{PUBLIC_BASE}/token/holders",
                 params={"tokenAddress": token_address, "limit": str(limit),
                         "offset": "0"}, timeout=12, headers=_headers())
    if not r:
        return {"error": "Solscan holders fetch failed", "holders": []}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Solscan holders parse failed", "holders": []}
    rows_in = d.get("data") if isinstance(d, dict) else d
    rows: list[dict] = []
    for row in (rows_in or []):
        if not isinstance(row, dict):
            continue
        rows.append({
            "address": row.get("address") or row.get("owner"),
            "amount": row.get("amount"),
            "decimals": row.get("decimals"),
            "rank": row.get("rank"),
        })
    out = {
        "source": "Solscan /token/holders",
        "token_address": token_address,
        "count": len(rows),
        "holders": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    # JUP token
    print(json.dumps(token_meta("JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"),
                     indent=2))
