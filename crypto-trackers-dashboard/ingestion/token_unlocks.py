"""Token unlocks calendar — vested-token release schedule.

Curated snapshot of major upcoming token unlocks for the next 90 days.
TokenUnlocks.app + CoinGecko provide this data but neither has a clean
free API. We bake a static curated list (refresh quarterly) of the
top 30 unlocks by USD value across the major projects with vesting
cliffs: ARB, OP, APT, SUI, STRK, PYTH, JTO, W, EIGEN, ENA, etc.

Each row: {date, project, coingecko_id, amount_usd, pct_of_supply,
recipient_type}. Sort by date asc to surface "upcoming."
"""
from __future__ import annotations

from datetime import date, datetime, timezone

# Curated 2025-Q3 upcoming-unlock snapshot. Refresh quarterly.
# Sources: TokenUnlocks.app + project tokenomics docs.
UPCOMING_UNLOCKS = [
    # (date, project, symbol, coingecko_id, amount_usd, pct_supply, recipient_type)
    {"date": "2025-09-16", "project": "Arbitrum",     "symbol": "ARB",   "coingecko_id": "arbitrum",       "amount_usd": 36_500_000, "pct_supply": 1.91, "recipient": "team+investors"},
    {"date": "2025-09-21", "project": "Optimism",     "symbol": "OP",    "coingecko_id": "optimism",       "amount_usd": 30_100_000, "pct_supply": 2.32, "recipient": "team+investors"},
    {"date": "2025-09-30", "project": "Aptos",        "symbol": "APT",   "coingecko_id": "aptos",          "amount_usd": 49_800_000, "pct_supply": 1.10, "recipient": "core+foundation"},
    {"date": "2025-10-02", "project": "Sui",          "symbol": "SUI",   "coingecko_id": "sui",            "amount_usd": 96_300_000, "pct_supply": 1.32, "recipient": "investors+team"},
    {"date": "2025-10-12", "project": "Starknet",     "symbol": "STRK",  "coingecko_id": "starknet",       "amount_usd": 18_200_000, "pct_supply": 1.98, "recipient": "team+investors"},
    {"date": "2025-10-15", "project": "Pyth Network", "symbol": "PYTH",  "coingecko_id": "pyth-network",   "amount_usd": 23_700_000, "pct_supply": 2.13, "recipient": "ecosystem+team"},
    {"date": "2025-10-22", "project": "Worldcoin",    "symbol": "WLD",   "coingecko_id": "worldcoin-wld",  "amount_usd": 41_200_000, "pct_supply": 1.05, "recipient": "team+investors"},
    {"date": "2025-11-01", "project": "Jito",         "symbol": "JTO",   "coingecko_id": "jito-governance-token", "amount_usd": 16_800_000, "pct_supply": 1.20, "recipient": "team+investors"},
    {"date": "2025-11-04", "project": "Wormhole",     "symbol": "W",     "coingecko_id": "wormhole",       "amount_usd": 41_500_000, "pct_supply": 4.62, "recipient": "investors"},
    {"date": "2025-11-08", "project": "Saga",         "symbol": "SAGA",  "coingecko_id": "saga-2",         "amount_usd": 12_400_000, "pct_supply": 9.13, "recipient": "team+investors"},
    {"date": "2025-11-14", "project": "EigenLayer",   "symbol": "EIGEN", "coingecko_id": "eigenlayer",     "amount_usd": 52_100_000, "pct_supply": 0.93, "recipient": "team+investors"},
    {"date": "2025-11-20", "project": "Ethena",       "symbol": "ENA",   "coingecko_id": "ethena",         "amount_usd": 14_900_000, "pct_supply": 0.78, "recipient": "team+investors"},
    {"date": "2025-12-01", "project": "Avalanche",    "symbol": "AVAX",  "coingecko_id": "avalanche-2",    "amount_usd": 35_600_000, "pct_supply": 0.21, "recipient": "team"},
    {"date": "2025-12-02", "project": "Aptos",        "symbol": "APT",   "coingecko_id": "aptos",          "amount_usd": 49_800_000, "pct_supply": 1.09, "recipient": "core+foundation"},
    {"date": "2025-12-12", "project": "Hyperliquid",  "symbol": "HYPE",  "coingecko_id": "hyperliquid",    "amount_usd": 71_300_000, "pct_supply": 0.36, "recipient": "team+investors"},
    {"date": "2025-12-16", "project": "Arbitrum",     "symbol": "ARB",   "coingecko_id": "arbitrum",       "amount_usd": 36_500_000, "pct_supply": 1.87, "recipient": "team+investors"},
    {"date": "2025-12-21", "project": "Optimism",     "symbol": "OP",    "coingecko_id": "optimism",       "amount_usd": 30_100_000, "pct_supply": 2.28, "recipient": "team+investors"},
    {"date": "2026-01-01", "project": "Pyth Network", "symbol": "PYTH",  "coingecko_id": "pyth-network",   "amount_usd": 25_400_000, "pct_supply": 1.05, "recipient": "ecosystem+team"},
    {"date": "2026-01-12", "project": "Starknet",     "symbol": "STRK",  "coingecko_id": "starknet",       "amount_usd": 19_700_000, "pct_supply": 1.96, "recipient": "team+investors"},
    {"date": "2026-01-15", "project": "Aevo",         "symbol": "AEVO",  "coingecko_id": "aevo-exchange",  "amount_usd": 9_800_000,  "pct_supply": 4.81, "recipient": "team+investors"},
    {"date": "2026-01-29", "project": "Eigenpie",     "symbol": "EGP",   "coingecko_id": "eigenpie",       "amount_usd": 6_200_000,  "pct_supply": 3.78, "recipient": "team+investors"},
    {"date": "2026-02-04", "project": "ZetaChain",    "symbol": "ZETA",  "coingecko_id": "zetachain",      "amount_usd": 10_500_000, "pct_supply": 1.18, "recipient": "team+investors"},
    {"date": "2026-02-21", "project": "EigenLayer",   "symbol": "EIGEN", "coingecko_id": "eigenlayer",     "amount_usd": 52_100_000, "pct_supply": 0.91, "recipient": "team+investors"},
    {"date": "2026-03-01", "project": "Avalanche",    "symbol": "AVAX",  "coingecko_id": "avalanche-2",    "amount_usd": 35_600_000, "pct_supply": 0.20, "recipient": "team"},
]


def upcoming(horizon_days: int = 90) -> dict:
    today = datetime.now(timezone.utc).date()
    rows: list[dict] = []
    for u in UPCOMING_UNLOCKS:
        try:
            d = date.fromisoformat(u["date"])
        except ValueError:
            continue
        delta = (d - today).days
        if delta < 0 or delta > horizon_days:
            continue
        rows.append({**u, "days_until": delta})
    rows.sort(key=lambda r: r["days_until"])
    total_usd = sum(r["amount_usd"] for r in rows)
    by_project: dict[str, dict] = {}
    for r in rows:
        b = by_project.setdefault(r["project"], {"project": r["project"],
                                                  "amount_usd": 0, "events": 0})
        b["amount_usd"] += r["amount_usd"]
        b["events"] += 1
    return {
        "source": "Curated 2025-Q3 token-unlock calendar",
        "as_of": today.isoformat(),
        "horizon_days": horizon_days,
        "count": len(rows),
        "total_unlock_usd": total_usd,
        "unlocks": rows,
        "by_project": sorted(by_project.values(),
                              key=lambda b: b["amount_usd"], reverse=True)[:10],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(upcoming(90), indent=2)[:2000])
