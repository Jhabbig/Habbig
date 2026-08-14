"""Bybit V5 public endpoints (perps + spot).

  - /v5/market/tickers?category=linear      - perps 24h tickers
  - /v5/market/tickers?category=spot        - spot 24h tickers
  - /v5/market/funding/history?category=linear&symbol=... - funding history
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

BYBIT_BASE = "https://api.bybit.com"


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tickers(category: str = "linear") -> dict:
    """``category`` ∈ {linear, spot, inverse, option}. linear = USDT perps."""
    cat = category if category in {"linear", "spot", "inverse", "option"} else "linear"
    hit = _cache.get(f"bybit_tick_{cat}", ttl_s=30)
    if hit is not None:
        return hit
    r = http_get(f"{BYBIT_BASE}/v5/market/tickers", params={"category": cat}, timeout=15)
    if not r:
        return {"error": "Bybit tickers fetch failed", "tickers": []}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Bybit tickers parse failed", "tickers": []}
    result = (d.get("result") or {}).get("list") or []
    norm = []
    for t in result:
        if not isinstance(t, dict):
            continue
        norm.append({
            "symbol": t.get("symbol"),
            "price": _f(t.get("lastPrice")),
            "bid": _f(t.get("bid1Price")),
            "ask": _f(t.get("ask1Price")),
            "change_pct_24h": _f(t.get("price24hPcnt")) * 100 if _f(t.get("price24hPcnt")) is not None else None,
            "high_24h": _f(t.get("highPrice24h")),
            "low_24h": _f(t.get("lowPrice24h")),
            "volume_24h": _f(t.get("volume24h")),
            "turnover_24h": _f(t.get("turnover24h")),
            "open_interest": _f(t.get("openInterest")),
            "funding_rate": _f(t.get("fundingRate")),
            "next_funding_time_ms": t.get("nextFundingTime"),
            "mark_price": _f(t.get("markPrice")),
            "index_price": _f(t.get("indexPrice")),
        })
    out = {
        "source": "Bybit /v5/market/tickers",
        "category": cat,
        "count": len(norm),
        "tickers": norm,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"bybit_tick_{cat}", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(tickers("linear"), indent=2)[:1500])
