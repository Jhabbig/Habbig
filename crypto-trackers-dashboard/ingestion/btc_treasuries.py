"""Bitcoin treasuries tracker.

Public companies + ETFs holding BTC. Sources:
  - https://bitbo.io/treasuries/ - HTML, would need scraping
  - https://bitcointreasuries.net/ - HTML scraping

Neither has a clean public JSON API. We bake in a curated snapshot of the
top holdings (updated 2025-Q3) so the dashboard has a meaningful "who
owns the supply" view without needing to scrape. The list is small and
moves slowly; refreshing it quarterly is fine.

Total BTC supply is hardcoded to 19.8M (close to current circulating in
late 2025). The dashboard shows each holding as both BTC and % of supply.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Top BTC holders, 2025-Q3 snapshot. Sourced from public 10-K filings
# (MSTR), bitcoin-treasuries.com aggregations, and ETF spot AUMs.
# This is intentionally a *static* baseline - the trader gets directionally
# correct holdings without depending on a flaky scrape.
HOLDINGS = [
    # Spot ETFs
    {"name": "BlackRock IBIT",                 "type": "ETF",      "btc": 720_000},
    {"name": "Fidelity FBTC",                  "type": "ETF",      "btc": 235_000},
    {"name": "Grayscale GBTC",                 "type": "ETF",      "btc": 215_000},
    {"name": "ARK 21Shares ARKB",              "type": "ETF",      "btc": 50_000},
    {"name": "Bitwise BITB",                   "type": "ETF",      "btc": 45_000},
    {"name": "Invesco Galaxy BTCO",            "type": "ETF",      "btc": 11_000},
    # Public companies
    {"name": "Strategy (MSTR)",                "type": "Public",   "btc": 615_000},
    {"name": "Marathon Digital (MARA)",        "type": "Public",   "btc": 47_500},
    {"name": "Riot Platforms (RIOT)",          "type": "Public",   "btc": 19_000},
    {"name": "Hut 8 (HUT)",                    "type": "Public",   "btc": 10_300},
    {"name": "CleanSpark (CLSK)",              "type": "Public",   "btc": 9_500},
    {"name": "Tesla (TSLA)",                   "type": "Public",   "btc": 9_700},
    {"name": "Block (SQ)",                     "type": "Public",   "btc": 8_300},
    {"name": "Coinbase (COIN)",                "type": "Public",   "btc": 9_500},
    # Governments
    {"name": "United States",                  "type": "Govt",     "btc": 200_000},
    {"name": "China",                          "type": "Govt",     "btc": 190_000},
    {"name": "United Kingdom",                 "type": "Govt",     "btc": 61_000},
    {"name": "Ukraine",                        "type": "Govt",     "btc": 46_000},
    {"name": "Bhutan",                         "type": "Govt",     "btc": 13_000},
    {"name": "El Salvador",                    "type": "Govt",     "btc": 6_100},
    # Private companies / funds
    {"name": "Block.one (private)",            "type": "Private",  "btc": 164_000},
]
TOTAL_SUPPLY_BTC = 19_800_000


def holdings_table() -> dict:
    rows = sorted(HOLDINGS, key=lambda r: r["btc"], reverse=True)
    total_tracked = sum(r["btc"] for r in rows)
    out_rows = []
    for r in rows:
        out_rows.append({
            **r,
            "pct_of_supply": round((r["btc"] / TOTAL_SUPPLY_BTC) * 100, 3),
            "pct_of_tracked": round((r["btc"] / total_tracked) * 100, 2),
        })
    return {
        "source": "Curated 2025-Q3 snapshot (10-Ks + ETF AUMs + government disclosures)",
        "as_of": "2025-09-30",
        "supply_btc": TOTAL_SUPPLY_BTC,
        "total_tracked_btc": total_tracked,
        "pct_of_supply_tracked": round((total_tracked / TOTAL_SUPPLY_BTC) * 100, 2),
        "by_type": _group_by_type(out_rows),
        "holdings": out_rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _group_by_type(rows: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for r in rows:
        t = r.get("type") or "?"
        b = out.setdefault(t, {"type": t, "btc": 0, "holders": 0})
        b["btc"] += r.get("btc", 0)
        b["holders"] += 1
    for v in out.values():
        v["pct_of_supply"] = round((v["btc"] / TOTAL_SUPPLY_BTC) * 100, 2)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["btc"]))


if __name__ == "__main__":
    import json
    print(json.dumps(holdings_table(), indent=2)[:1500])
