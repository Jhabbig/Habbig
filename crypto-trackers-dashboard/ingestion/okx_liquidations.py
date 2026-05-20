"""OKX public liquidation orders.

  - /api/v5/public/liquidation-orders?instType=SWAP&state=filled

Public, no auth. Returns the latest force-liquidated positions across
USDT-margined perp swaps. We sum by symbol + side for the Coinglass-style
multi-venue panel.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

BASE = "https://www.okx.com"


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def recent_liquidations(inst_type: str = "SWAP", limit: int = 100) -> dict:
    it = inst_type if inst_type in {"SWAP", "FUTURES", "MARGIN"} else "SWAP"
    hit = _cache.get(f"okx_liq_{it}_{limit}", ttl_s=60)
    if hit is not None:
        return hit
    r = http_get(f"{BASE}/api/v5/public/liquidation-orders",
                 params={"instType": it, "state": "filled", "limit": str(limit)},
                 timeout=15)
    if not r:
        return {"error": "OKX liquidation fetch failed", "rows": [], "count": 0}
    try:
        d = r.json()
    except ValueError:
        return {"error": "OKX liquidation parse failed", "rows": [], "count": 0}
    rows_in = d.get("data") or []
    # OKX nests detail rows under each instrument entry
    parsed: list[dict] = []
    by_symbol: dict[str, dict] = {}
    for inst in rows_in:
        if not isinstance(inst, dict):
            continue
        inst_id = inst.get("instId") or "?"
        for detail in (inst.get("details") or []):
            if not isinstance(detail, dict):
                continue
            side = detail.get("side")     # "buy"/"sell" — sell means a long got liquidated
            qty = _f(detail.get("sz"))    # contracts
            price = _f(detail.get("bkPx") or detail.get("fillPx"))
            notional = (qty or 0) * (price or 0)
            ts = _f(detail.get("ts"))
            item = {
                "symbol": inst_id,
                "side": (side or "").upper(),
                "qty": qty,
                "price": price,
                "notional_usd": notional,
                "ts_ms": int(ts) if ts is not None else None,
            }
            parsed.append(item)
            s = by_symbol.setdefault(inst_id, {
                "symbol": inst_id, "long_liq_usd": 0.0, "short_liq_usd": 0.0,
                "total_usd": 0.0, "count": 0,
            })
            s["count"] += 1
            s["total_usd"] += notional
            if item["side"] == "SELL":
                s["long_liq_usd"] += notional
            elif item["side"] == "BUY":
                s["short_liq_usd"] += notional

    agg = sorted(by_symbol.values(), key=lambda x: x["total_usd"], reverse=True)
    parsed.sort(key=lambda r: r.get("ts_ms") or 0, reverse=True)
    biggest = max(parsed, key=lambda r: r.get("notional_usd") or 0) if parsed else None
    out = {
        "source": "OKX /api/v5/public/liquidation-orders",
        "count": len(parsed),
        "by_symbol_top": agg[:20],
        "recent": parsed[:30],
        "biggest": biggest,
        "total_notional_usd": sum(x["total_usd"] for x in agg),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"okx_liq_{it}_{limit}", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(recent_liquidations(), indent=2)[:1500])
