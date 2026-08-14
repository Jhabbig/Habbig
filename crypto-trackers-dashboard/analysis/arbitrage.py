"""Cross-exchange spread + arbitrage scanner.

For every coin where we have a price on >= 2 exchanges, compute:

  - min/max price across venues
  - spread_pct = (max - min) / mid
  - which venue is highest / lowest
  - estimated arb-after-fees (using each exchange's published taker fee)

Real-world arbitrage is rarely profitable above a fraction of a percent
because of withdrawal fees + on-chain latency + venue-specific fee tiers.
The dashboard surfaces the raw spread; whether it's actionable depends on
the user's account state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# Conservative taker fees (post-discount, retail-default tier). Editing
# this table tightens or loosens the "actionable arb" threshold.
TAKER_FEE_PCT = {
    "binance":  0.10,
    "coinbase": 0.40,
    "kraken":   0.26,
    "bybit":    0.075,
    "okx":      0.10,
}


# Quote currencies that are USD-equivalent (1:1 with the dollar). Prices
# quoted in anything else (EUR, GBP, ...) must never be price-compared
# against USD rows — the FX gap would surface as a large phantom "spread".
USD_QUOTES = {"USDT", "USDC", "USD", "USDP", "BUSD"}

# Longer quotes first so e.g. BTCBUSD parses as BTC/BUSD, not BTCB/USD.
_KNOWN_QUOTES = ("USDT", "USDC", "USDP", "BUSD", "USD", "EUR", "GBP")


def _split_symbol(symbol: str) -> tuple[Optional[str], Optional[str]]:
    """Reduce exchange-specific symbol formats to (base, quote).

    Examples:
       BTCUSDT   -> (BTC, USDT)
       BTC-USD   -> (BTC, USD)
       XBTUSD    -> (BTC, USD)       (Kraken's "XBT" convention)
       BTC-USDT-SWAP -> (BTC, USDT)  (OKX perps)
       BTC-EUR   -> (BTC, EUR)

    quote is None when no known quote suffix is recognised.
    """
    if not symbol:
        return None, None
    s = symbol.upper()
    # Strip OKX's -SWAP suffix
    s = s.replace("-SWAP", "")
    # Replace XBT (Kraken) with BTC
    s = s.replace("XBT", "BTC")
    # Strip the quote currency
    for quote in _KNOWN_QUOTES:
        if s.endswith("-" + quote) or s.endswith(quote):
            if s.endswith("-" + quote):
                base = s[: -len(quote) - 1]
            else:
                base = s[: -len(quote)]
            return (base or None), quote
    return s, None


def _normalise_symbol(symbol: str) -> Optional[str]:
    """Reduce exchange-specific symbol formats to a base ticker (quote dropped)."""
    return _split_symbol(symbol)[0]


def _row_for(exchange: str, ticker: dict, *, symbol_field: str,
              price_field: str) -> Optional[dict]:
    sym = ticker.get(symbol_field) or ticker.get("symbol")
    base, quote = _split_symbol(sym)
    if not base:
        return None
    # Only USD-equivalent quotes are comparable: an EUR/GBP-denominated (or
    # crypto-quoted) price grouped with USD rows fabricates arbitrage.
    if quote not in USD_QUOTES:
        return None
    price = ticker.get(price_field) if price_field else ticker.get("price")
    if price is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return {"exchange": exchange, "base": base, "symbol_full": sym, "price": price}


def cross_exchange_spreads(
    *, binance_spot: Optional[dict] = None,
    coinbase_tickers: Optional[list[dict]] = None,
    kraken_tickers: Optional[list[dict]] = None,
    bybit_tickers: Optional[dict] = None,
    okx_tickers: Optional[dict] = None,
    min_volume_usd: float = 500_000,
    top_n: int = 50,
) -> dict:
    """Join all available tickers by base symbol and surface spreads."""
    rows: list[dict] = []

    # Binance spot uses suffix-based symbols (BTCUSDT)
    if binance_spot and not binance_spot.get("error"):
        for t in binance_spot.get("tickers") or []:
            sym = t.get("symbol") or ""
            if not (sym.endswith("USDT") or sym.endswith("USDC") or sym.endswith("USD")):
                continue
            vol = (t.get("quote_volume") or 0)
            if vol < min_volume_usd:
                continue
            row = _row_for("binance", t, symbol_field="symbol", price_field="price")
            if row:
                row["volume_usd"] = vol
                rows.append(row)

    # Coinbase tickers come in as dicts keyed by product_id
    for t in coinbase_tickers or []:
        if not t or t.get("error"):
            continue
        vol = (t.get("volume_24h") or 0) * (t.get("price") or 0)
        if vol < min_volume_usd:
            continue
        row = _row_for("coinbase", {"symbol": t.get("product_id"), "price": t.get("price")},
                       symbol_field="symbol", price_field="price")
        if row:
            row["volume_usd"] = vol
            rows.append(row)

    # Kraken (one ticker per pair)
    for t in kraken_tickers or []:
        if not t or t.get("error"):
            continue
        vol = (t.get("volume_24h") or 0) * (t.get("price") or 0)
        if vol < min_volume_usd:
            continue
        row = _row_for("kraken", {"symbol": t.get("pair"), "price": t.get("price")},
                       symbol_field="symbol", price_field="price")
        if row:
            row["volume_usd"] = vol
            rows.append(row)

    # Bybit linear (perps)
    if bybit_tickers and not bybit_tickers.get("error"):
        for t in bybit_tickers.get("tickers") or []:
            vol = (t.get("turnover_24h") or 0)
            if vol < min_volume_usd:
                continue
            row = _row_for("bybit", t, symbol_field="symbol", price_field="price")
            if row:
                row["volume_usd"] = vol
                rows.append(row)

    # OKX
    if okx_tickers and not okx_tickers.get("error"):
        for t in okx_tickers.get("tickers") or []:
            vol = (t.get("turnover_24h") or 0)
            if vol < min_volume_usd:
                continue
            row = _row_for("okx", t, symbol_field="symbol", price_field="price")
            if row:
                row["volume_usd"] = vol
                rows.append(row)

    # Group by base symbol
    by_base: dict[str, list[dict]] = {}
    for r in rows:
        by_base.setdefault(r["base"], []).append(r)

    spreads: list[dict] = []
    for base, group in by_base.items():
        if len(group) < 2:
            continue
        prices = [g["price"] for g in group]
        lo = min(prices)
        hi = max(prices)
        if lo <= 0:
            continue
        spread_pct = (hi - lo) / ((hi + lo) / 2.0) * 100.0
        # Net of round-trip taker fees on both venues
        lo_row = next(g for g in group if g["price"] == lo)
        hi_row = next(g for g in group if g["price"] == hi)
        fee_round_trip = TAKER_FEE_PCT.get(lo_row["exchange"], 0.20) + TAKER_FEE_PCT.get(hi_row["exchange"], 0.20)
        net_pct = spread_pct - fee_round_trip
        spreads.append({
            "base": base,
            "venues": [g["exchange"] for g in group],
            "low_venue": lo_row["exchange"],
            "low_price": lo,
            "high_venue": hi_row["exchange"],
            "high_price": hi,
            "spread_pct": round(spread_pct, 3),
            "round_trip_fees_pct": round(fee_round_trip, 3),
            "net_arb_pct": round(net_pct, 3),
            "total_volume_usd": sum(g.get("volume_usd") or 0 for g in group),
        })

    spreads.sort(key=lambda x: x["net_arb_pct"], reverse=True)
    return {
        "spreads_top": spreads[:top_n],
        "actionable_count": sum(1 for s in spreads if s["net_arb_pct"] > 0.05),
        "min_volume_usd": min_volume_usd,
        "exchanges_used": sorted({r["exchange"] for r in rows}),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    from ingestion import binance, bybit, okx, coinbase, kraken
    spot = binance.spot_ticker_24h()
    by = bybit.tickers("spot")
    okx_spot = okx.tickers("SPOT")
    cb = [coinbase.ticker(p) for p in ("BTC-USD", "ETH-USD", "SOL-USD")]
    kr = [kraken.ticker(p) for p in ("XBTUSD", "ETHUSD", "SOLUSD")]
    import json
    print(json.dumps(cross_exchange_spreads(
        binance_spot=spot, coinbase_tickers=cb, kraken_tickers=kr,
        bybit_tickers=by, okx_tickers=okx_spot), indent=2)[:2500])
