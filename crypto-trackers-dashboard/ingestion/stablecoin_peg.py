"""Stablecoin peg monitor.

Pulls live prices for the top stablecoins from CoinGecko (via the
existing universe cache) and computes deviation from $1.00. Surfaces:
  - Worst peg deviation right now
  - Stables sorted by |deviation|
  - 24h change of the peg (useful for "is the de-peg recovering?")

Combined with `defillama.stablecoins()` (supply + chains) this gives the
full picture without extra upstream calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache, coingecko

# CoinGecko IDs for the top stables (2025-Q3 snapshot).
TRACKED_STABLES = [
    ("USDT", "tether"),
    ("USDC", "usd-coin"),
    ("DAI",  "dai"),
    ("USDe", "ethena-usde"),
    ("USDS", "sky-dollar"),       # formerly DAI's Maker upgrade
    ("PYUSD","paypal-usd"),
    ("FDUSD","first-digital-usd"),
    ("TUSD", "true-usd"),
    ("USDP", "paxos-standard"),
    ("FRAX", "frax"),
    ("LUSD", "liquity-usd"),
    ("crvUSD","crvusd"),
    ("GHO",  "gho"),
    ("USDD", "usdd"),
    ("BUSD", "binance-usd"),
]

PEG = 1.0


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def peg_status() -> dict:
    hit = _cache.get("stable_peg", ttl_s=60)
    if hit is not None:
        return hit
    univ = coingecko.universe(500)
    coins_by_id = {c.get("id"): c for c in (univ.get("coins") or [])}
    rows: list[dict] = []
    for sym, cg_id in TRACKED_STABLES:
        c = coins_by_id.get(cg_id)
        if not c:
            continue
        price = _f(c.get("current_price"))
        if price is None:
            continue
        deviation = price - PEG
        deviation_bps = deviation * 10_000
        rows.append({
            "symbol": sym,
            "coingecko_id": cg_id,
            "price": round(price, 6),
            "deviation_from_peg": round(deviation, 6),
            "deviation_bps": round(deviation_bps, 1),
            "abs_deviation_bps": round(abs(deviation_bps), 1),
            "change_24h": _f(c.get("change_24h")),
            "market_cap_usd": _f(c.get("market_cap")),
            "image": c.get("image"),
        })
    rows.sort(key=lambda r: r["abs_deviation_bps"], reverse=True)
    worst = rows[0] if rows else None
    out = {
        "source": "CoinGecko universe + curated stablecoin list",
        "count": len(rows),
        "worst_peg": worst,
        "any_depeg_over_50_bps": [r["symbol"] for r in rows if r["abs_deviation_bps"] > 50],
        "any_depeg_over_25_bps": [r["symbol"] for r in rows if r["abs_deviation_bps"] > 25],
        "rows": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("stable_peg", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(peg_status(), indent=2)[:2000])
