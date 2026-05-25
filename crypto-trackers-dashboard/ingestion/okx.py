"""OKX V5 public endpoints.

  - /api/v5/market/tickers?instType=SWAP   - perps 24h tickers
  - /api/v5/market/tickers?instType=SPOT   - spot 24h tickers
  - /api/v5/public/funding-rate?instId=... - funding rate per instrument
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

OKX_BASE = "https://www.okx.com"


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tickers(inst_type: str = "SWAP") -> dict:
    """``inst_type`` ∈ {SPOT, SWAP, FUTURES, OPTION}. SWAP = perps."""
    it = inst_type if inst_type in {"SPOT", "SWAP", "FUTURES", "OPTION"} else "SWAP"
    hit = _cache.get(f"okx_tick_{it}", ttl_s=30)
    if hit is not None:
        return hit
    r = http_get(f"{OKX_BASE}/api/v5/market/tickers",
                 params={"instType": it}, timeout=15)
    if not r:
        return {"error": "OKX tickers fetch failed", "tickers": []}
    try:
        d = r.json()
    except ValueError:
        return {"error": "OKX tickers parse failed", "tickers": []}
    rows = d.get("data") or []
    norm = []
    for t in rows:
        if not isinstance(t, dict):
            continue
        last = _f(t.get("last"))
        open24 = _f(t.get("open24h"))
        chg = None
        if last is not None and open24 not in (None, 0):
            chg = (last - open24) / open24 * 100
        norm.append({
            "symbol": t.get("instId"),
            "price": last,
            "bid": _f(t.get("bidPx")),
            "ask": _f(t.get("askPx")),
            "open_24h": open24,
            "high_24h": _f(t.get("high24h")),
            "low_24h": _f(t.get("low24h")),
            "change_pct_24h": chg,
            "volume_24h": _f(t.get("vol24h")),
            "turnover_24h": _f(t.get("volCcy24h")),
        })
    out = {
        "source": "OKX /api/v5/market/tickers",
        "inst_type": it,
        "count": len(norm),
        "tickers": norm,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"okx_tick_{it}", out)
    return out


def funding_rate(inst_id: str = "BTC-USDT-SWAP") -> dict:
    iid = inst_id.upper()
    hit = _cache.get(f"okx_funding_{iid}", ttl_s=60)
    if hit is not None:
        return hit
    r = http_get(f"{OKX_BASE}/api/v5/public/funding-rate",
                 params={"instId": iid}, timeout=10)
    if not r:
        return {"error": "OKX funding rate fetch failed", "inst_id": iid}
    try:
        d = r.json()
    except ValueError:
        return {"error": "OKX funding parse failed", "inst_id": iid}
    rows = d.get("data") or []
    if not rows:
        return {"error": "OKX funding empty", "inst_id": iid}
    row = rows[0]
    out = {
        "source": "OKX /api/v5/public/funding-rate",
        "inst_id": iid,
        "funding_rate": _f(row.get("fundingRate")),
        "next_funding_time_ms": _f(row.get("nextFundingTime")),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"okx_funding_{iid}", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(tickers("SWAP"), indent=2)[:1200])
