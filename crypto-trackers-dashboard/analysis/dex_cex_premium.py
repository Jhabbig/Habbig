"""DEX-vs-CEX premium per token.

For tokens we track on both DEX (DefiLlama prices) and CEX (Binance spot
24h ticker), compute:

  premium_bps = (dex_price - cex_price) / cex_price * 10000

Positive = DEX is richer than CEX (sign of CEX selling pressure or DEX
buy demand). Large absolute premium is a stat-arb signal — typically
unwound by bridge/swap loops within hours, but during stress events
(de-pegs, exchange outages) premiums can persist.
"""
from __future__ import annotations

from typing import Optional


def compute(*, dex_prices: dict, binance_spot: dict) -> dict:
    """Returns rows with cex_price, dex_price, premium_bps, sign."""
    if not dex_prices or dex_prices.get("error"):
        return {"error": "dex prices unavailable", "rows": []}
    if not binance_spot or binance_spot.get("error"):
        return {"error": "binance spot unavailable", "rows": []}
    cex_by_base: dict[str, float] = {}
    for t in binance_spot.get("tickers") or []:
        sym = t.get("symbol") or ""
        if sym.endswith("USDT") and t.get("price"):
            cex_by_base[sym[:-4]] = t["price"]
    rows = []
    for tok in dex_prices.get("tokens") or []:
        sym = tok.get("symbol") or ""
        dex_price = tok.get("price_usd")
        # Map a few aliases
        cex_lookup = {"WBTC": "BTC", "WETH": "ETH", "WBNB": "BNB"}.get(sym, sym)
        cex_price = cex_by_base.get(cex_lookup)
        if dex_price is None or cex_price is None or cex_price <= 0:
            continue
        premium_bps = (dex_price - cex_price) / cex_price * 10000
        rows.append({
            "symbol": sym,
            "cex_lookup": cex_lookup,
            "chain": tok.get("chain"),
            "dex_price": round(dex_price, 6),
            "cex_price": round(cex_price, 6),
            "premium_bps": round(premium_bps, 2),
            "sign": "DEX_RICH" if premium_bps > 0 else "CEX_RICH",
            "dex_confidence": tok.get("confidence"),
        })
    rows.sort(key=lambda r: abs(r["premium_bps"]), reverse=True)
    return {
        "rows": rows,
        "count": len(rows),
        "actionable_count": sum(1 for r in rows if abs(r["premium_bps"]) > 20),  # 20 bps threshold
    }
