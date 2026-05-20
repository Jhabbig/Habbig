"""Bybit recent-trades-as-liquidation-proxy.

Bybit deprecated public REST liquidation endpoints in 2022 - only their
websocket stream carries live force-orders. To stay within our REST-poll
architecture we use the next-best public signal: large recent-trades on
perp instruments. A trade that fills with isBuyerMaker=false and
size > $1M on a perp is almost always a long getting wrecked.

This is an imperfect proxy but covers the gap for Bybit until v1.0
swaps in a real websocket consumer.

Endpoint: /v5/market/recent-trade?category=linear&symbol=BTCUSDT&limit=200
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

BASE = "https://api.bybit.com"

# Symbols to probe. Hand-picked top-volume Bybit perps.
TRACKED_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
    "BNBUSDT", "LINKUSDT", "AVAXUSDT", "MATICUSDT", "DOTUSDT",
    "ARBUSDT", "OPUSDT", "ATOMUSDT", "APTUSDT", "SUIUSDT",
)

# Minimum trade size in USD to qualify as a "probable liquidation"
MIN_NOTIONAL_USD = 1_000_000


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_trades(symbol: str, limit: int = 200) -> list[dict]:
    r = http_get(f"{BASE}/v5/market/recent-trade",
                 params={"category": "linear", "symbol": symbol,
                         "limit": str(limit)}, timeout=10)
    if not r:
        return []
    try:
        d = r.json()
    except ValueError:
        return []
    return ((d.get("result") or {}).get("list") or [])


def probable_liquidations() -> dict:
    """Aggregate large recent trades across tracked Bybit symbols.

    For each trade with size_usd >= MIN_NOTIONAL_USD we flag it as a
    "probable_liq" with side = "long_liq" if it's a market sell
    (isBuyerMaker=true => taker was selling) or "short_liq" if market buy.
    """
    hit = _cache.get("bybit_proxy_liq", ttl_s=60)
    if hit is not None:
        return hit
    by_symbol: dict[str, dict] = {}
    parsed: list[dict] = []
    for sym in TRACKED_SYMBOLS:
        trades = _fetch_trades(sym, limit=200)
        for t in trades:
            if not isinstance(t, dict):
                continue
            price = _f(t.get("price"))
            size = _f(t.get("size"))
            if price is None or size is None:
                continue
            notional = price * size
            if notional < MIN_NOTIONAL_USD:
                continue
            # Bybit recent-trade has side: "Buy" or "Sell" describing the
            # *taker* side. Taker-sell hitting bid => long getting wrecked.
            side_taker = (t.get("side") or "").upper()
            # In Bybit V5, side describes the taker. "Sell" = taker sold
            # (hits bid) = market-sell. Treat as long liquidation proxy.
            if side_taker == "SELL":
                liq_side = "long_liq"
            elif side_taker == "BUY":
                liq_side = "short_liq"
            else:
                continue
            ts_ms = _f(t.get("time"))
            item = {
                "symbol": sym,
                "liq_side": liq_side,
                "price": price,
                "size": size,
                "notional_usd": notional,
                "ts_ms": int(ts_ms) if ts_ms else None,
            }
            parsed.append(item)
            s = by_symbol.setdefault(sym, {
                "symbol": sym, "long_liq_usd": 0.0, "short_liq_usd": 0.0,
                "total_usd": 0.0, "count": 0,
            })
            s["count"] += 1
            s["total_usd"] += notional
            if liq_side == "long_liq":
                s["long_liq_usd"] += notional
            else:
                s["short_liq_usd"] += notional

    agg = sorted(by_symbol.values(), key=lambda x: x["total_usd"], reverse=True)
    parsed.sort(key=lambda r: r.get("ts_ms") or 0, reverse=True)
    biggest = max(parsed, key=lambda r: r.get("notional_usd") or 0) if parsed else None
    out = {
        "source": f"Bybit recent-trade >= ${MIN_NOTIONAL_USD/1e6:.1f}M proxy",
        "min_notional_usd": MIN_NOTIONAL_USD,
        "symbols_probed": len(TRACKED_SYMBOLS),
        "count": len(parsed),
        "by_symbol_top": agg[:20],
        "recent": parsed[:30],
        "biggest": biggest,
        "total_notional_usd": sum(x["total_usd"] for x in agg),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "note": "Trade-size proxy, not true liquidation feed. Real Bybit liqs require websocket.",
    }
    _cache.put("bybit_proxy_liq", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(probable_liquidations(), indent=2)[:1500])
