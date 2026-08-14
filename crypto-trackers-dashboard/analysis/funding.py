"""Funding-rate aggregator across perp venues (Binance, Bybit, OKX).

Funding rates are paid every 8 hours on most perp venues. Positive funding
means longs pay shorts; negative is the opposite. Big absolute funding
(|rate| > 0.05% per 8h, i.e. ~150% annualised) is a contrarian signal -
positions are crowded and a squeeze is likely.

We collect funding from every perp venue we cover, normalise to "per-8h"
basis (Binance + Bybit already are; OKX returns next-funding so we treat
it as the same cadence), and join by normalised base symbol.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from analysis.arbitrage import _normalise_symbol


def collect(
    *, binance_premium: Optional[dict] = None,
    bybit_tickers: Optional[dict] = None,
    okx_tickers: Optional[dict] = None,
) -> dict:
    """Returns a list of {base, venues[], min/max/median funding_rate}.

    Each row also has a "spread_bps" - max - min funding across venues -
    which is useful for cash-and-carry / pair trades.
    """
    by_base: dict[str, list[dict]] = {}

    # Binance
    if binance_premium and not binance_premium.get("error"):
        for r in binance_premium.get("rows") or []:
            sym = r.get("symbol")
            if not sym or not sym.endswith("USDT"):
                continue
            base = _normalise_symbol(sym)
            if not base:
                continue
            fr = r.get("funding_rate")
            if fr is None:
                continue
            by_base.setdefault(base, []).append({
                "venue": "binance",
                "rate": fr,
                "mark_price": r.get("mark_price"),
                "next_funding_time_ms": r.get("next_funding_time_ms"),
            })

    # Bybit linear (USDT perps)
    if bybit_tickers and not bybit_tickers.get("error"):
        for t in bybit_tickers.get("tickers") or []:
            sym = t.get("symbol") or ""
            if not sym.endswith("USDT"):
                continue
            base = _normalise_symbol(sym)
            if not base:
                continue
            fr = t.get("funding_rate")
            if fr is None:
                continue
            by_base.setdefault(base, []).append({
                "venue": "bybit",
                "rate": fr,
                "mark_price": t.get("mark_price"),
                "next_funding_time_ms": t.get("next_funding_time_ms"),
            })

    # OKX swaps - we only have ticker (no funding rate) from the bulk
    # endpoint; per-instrument funding is available at /api/v5/public/funding-rate
    # but we don't fan that out here. Bulk funding can be added in a v0.x.

    rows: list[dict] = []
    for base, venues in by_base.items():
        rates = sorted(v["rate"] for v in venues)
        if not rates:
            continue
        n = len(rates)
        median = rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) / 2.0
        rows.append({
            "base": base,
            "venues": [v["venue"] for v in venues],
            "rates": {v["venue"]: v["rate"] for v in venues},
            "min_rate": rates[0],
            "max_rate": rates[-1],
            "median_rate": median,
            "spread_bps": round((rates[-1] - rates[0]) * 10000, 2),  # bp = basis point of unit rate
            "annualised_at_median_pct": round(median * 3 * 365 * 100, 1),  # 3 funding intervals/day * 365
        })
    rows.sort(key=lambda r: abs(r.get("median_rate") or 0), reverse=True)

    return {
        "rows_top": rows[:60],
        "total_symbols": len(rows),
        "high_funding_long_count": sum(1 for r in rows if (r.get("median_rate") or 0) > 0.0005),
        "high_funding_short_count": sum(1 for r in rows if (r.get("median_rate") or 0) < -0.0005),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
