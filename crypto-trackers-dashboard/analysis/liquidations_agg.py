"""Multi-venue liquidation aggregator (Binance + OKX).

Joins per-venue liquidation feeds on a normalised base symbol
(stripping USDT/USDC/USD quote currency, -SWAP suffix, XBT-vs-BTC alias)
to produce a Coinglass-lite cross-exchange table.

Returns:
  - rows: per-coin combined long-liq / short-liq / total
  - total_notional_usd
  - top_long_squeeze: coin with biggest long liquidation (price down event)
  - top_short_squeeze: coin with biggest short liquidation (price up event)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from analysis.arbitrage import _normalise_symbol


def aggregate(*, binance: Optional[dict] = None, okx: Optional[dict] = None) -> dict:
    by_base: dict[str, dict] = {}
    venues_used: list[str] = []

    def _ingest(rows: list[dict], venue: str) -> None:
        venues_used.append(venue)
        for r in rows or []:
            sym = r.get("symbol")
            base = _normalise_symbol(sym or "")
            if not base:
                continue
            agg = by_base.setdefault(base, {
                "base": base, "venues": [], "long_liq_usd": 0.0, "short_liq_usd": 0.0,
                "total_usd": 0.0, "count": 0, "by_venue": {},
            })
            if venue not in agg["venues"]:
                agg["venues"].append(venue)
            agg["count"] += r.get("count", 0)
            agg["total_usd"] += r.get("total_usd", 0)
            agg["long_liq_usd"] += r.get("long_liq_usd", 0)
            agg["short_liq_usd"] += r.get("short_liq_usd", 0)
            agg["by_venue"][venue] = {
                "total_usd": r.get("total_usd", 0),
                "long_liq_usd": r.get("long_liq_usd", 0),
                "short_liq_usd": r.get("short_liq_usd", 0),
                "count": r.get("count", 0),
            }

    if binance and not binance.get("error"):
        _ingest(binance.get("by_symbol_top") or [], "binance")
    if okx and not okx.get("error"):
        _ingest(okx.get("by_symbol_top") or [], "okx")

    rows = sorted(by_base.values(), key=lambda x: x["total_usd"], reverse=True)
    long_squeeze = max(rows, key=lambda r: r["long_liq_usd"], default=None)
    short_squeeze = max(rows, key=lambda r: r["short_liq_usd"], default=None)

    return {
        "venues_used": venues_used,
        "total_notional_usd": sum(r["total_usd"] for r in rows),
        "rows": rows[:25],
        "top_long_squeeze": long_squeeze,
        "top_short_squeeze": short_squeeze,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
