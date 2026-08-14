"""Recent Binance USDT-perp liquidations.

Binance's ``/fapi/v1/allForceOrders`` returns the last 100 force-liquidated
positions across all symbols. We aggregate by symbol + side + total notional
to produce the equivalent of a Coinglass-lite liquidation feed.

For real-time streaming use the websocket ``!forceOrder@arr``; for now we
poll the REST endpoint at 60s cadence which is enough for the dashboard.

Caveats: Binance recently restricted historical force-orders to authenticated
keys. The unauthenticated endpoint may return only the latest few entries
or a 403. We degrade gracefully.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

BASE = "https://fapi.binance.com"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def recent_liquidations(limit: int = 100) -> dict:
    limit = max(10, min(limit, 1000))
    hit = _cache.get(f"binance_liq_{limit}", ttl_s=60)
    if hit is not None:
        return hit
    r = http_get(f"{BASE}/fapi/v1/allForceOrders",
                 params={"limit": str(limit)}, timeout=15)
    if not r:
        return {"error": "Binance liquidation fetch failed", "rows": [], "count": 0,
                "note": "Endpoint may require an API key as of 2024+. Set BINANCE_API_KEY."}
    try:
        rows = r.json()
    except ValueError:
        return {"error": "Binance liquidation parse failed", "rows": [], "count": 0}
    if isinstance(rows, dict):
        return {"error": rows.get("msg") or "Binance returned error",
                "rows": [], "count": 0}

    by_symbol: dict[str, dict] = {}
    parsed: list[dict] = []
    for r_ in rows:
        if not isinstance(r_, dict):
            continue
        sym = r_.get("symbol")
        side = r_.get("side")  # BUY = short liq'd, SELL = long liq'd
        qty = _f(r_.get("origQty") or r_.get("executedQty"))
        price = _f(r_.get("price") or r_.get("avgPrice"))
        notional = (qty or 0) * (price or 0)
        ts = r_.get("time") or r_.get("updateTime")
        item = {
            "symbol": sym,
            "side": side,
            "qty": qty,
            "price": price,
            "notional_usd": notional,
            "ts_ms": ts,
        }
        parsed.append(item)
        s = by_symbol.setdefault(sym or "?", {
            "symbol": sym, "long_liq_usd": 0.0, "short_liq_usd": 0.0,
            "total_usd": 0.0, "count": 0,
        })
        s["count"] += 1
        s["total_usd"] += notional
        if side == "SELL":  # long getting liquidated (force sell)
            s["long_liq_usd"] += notional
        elif side == "BUY":  # short getting liquidated (force buy)
            s["short_liq_usd"] += notional

    agg = sorted(by_symbol.values(), key=lambda x: x["total_usd"], reverse=True)
    parsed.sort(key=lambda r: r.get("ts_ms") or 0, reverse=True)
    biggest = max(parsed, key=lambda r: r.get("notional_usd") or 0) if parsed else None
    out = {
        "source": "Binance /fapi/v1/allForceOrders",
        "count": len(parsed),
        "by_symbol_top": agg[:20],
        "recent": parsed[:30],
        "biggest": biggest,
        "total_notional_usd": sum(x["total_usd"] for x in agg),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"binance_liq_{limit}", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(recent_liquidations(), indent=2)[:1500])
