"""Kraken public REST endpoints.

  - /0/public/Ticker?pair=...  - current price + bid/ask + 24h o/h/l/v
  - /0/public/AssetPairs       - every tradable pair
  - /0/public/Depth?pair=...   - L2 orderbook

Kraken's pair naming is idiosyncratic (XBT for BTC, XXBT/USD for some
spot pairs). We use the user-facing pair (e.g. "XBTUSD") in our cache
keys and translate when needed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

KRAKEN_BASE = "https://api.kraken.com"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ticker(pair: str = "XBTUSD") -> dict:
    p = pair.upper()
    hit = _cache.get(f"kraken_tick_{p}", ttl_s=10)
    if hit is not None:
        return hit
    r = http_get(f"{KRAKEN_BASE}/0/public/Ticker", params={"pair": p}, timeout=10)
    if not r:
        return {"error": "Kraken ticker failed", "pair": p}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Kraken ticker parse failed", "pair": p}
    if d.get("error"):
        return {"error": d["error"][0] if d["error"] else "Kraken error", "pair": p}
    result = d.get("result") or {}
    if not result:
        return {"error": "Kraken empty result", "pair": p}
    # Kraken returns one key per actual pair name (e.g. "XXBTZUSD" for XBTUSD)
    krak_pair, body = next(iter(result.items()))
    # body has c/b/a/v/p/t/l/h/o:
    #   c=[last_price, lot_volume], b=[bid_price, ...], a=[ask_price, ...]
    #   v=[24h_volume_today, 24h_volume], o=opening_today
    out = {
        "source": "Kraken /0/public/Ticker",
        "pair": p,
        "kraken_pair": krak_pair,
        "price": _f((body.get("c") or [None])[0]),
        "bid": _f((body.get("b") or [None])[0]),
        "ask": _f((body.get("a") or [None])[0]),
        "volume_24h": _f((body.get("v") or [None, None])[1]),
        "high_24h": _f((body.get("h") or [None, None])[1]),
        "low_24h": _f((body.get("l") or [None, None])[1]),
        "open_24h": _f(body.get("o")),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"kraken_tick_{p}", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(ticker("XBTUSD"), indent=2))
