"""Sector grouping for the universe table.

CoinGecko's universe response doesn't include categories per-coin (the
detail endpoint does). We bake a curated mapping of major coins to
sectors so the home page can group the top-N by Layer-1 / Layer-2 / DeFi
/ Memes / AI / RWA / etc. without needing a per-coin detail call.
"""
from __future__ import annotations


# Curated coin-id -> sector mapping (2025-Q3). Coins not in this map fall
# into "other". Keeps the home page sector view snappy and stable.
COIN_SECTORS = {
    # Layer-1 smart-contract platforms
    "bitcoin": "L1", "ethereum": "L1", "solana": "L1", "binancecoin": "L1",
    "cardano": "L1", "avalanche-2": "L1", "tron": "L1", "polkadot": "L1",
    "cosmos": "L1", "near": "L1", "aptos": "L1", "sui": "L1",
    "internet-computer": "L1", "hedera-hashgraph": "L1", "algorand": "L1",
    "stellar": "L1", "litecoin": "L1", "bitcoin-cash": "L1", "monero": "L1",
    "ethereum-classic": "L1", "tezos": "L1", "kaspa": "L1", "ton": "L1",
    "the-open-network": "L1", "celestia": "L1",
    # Layer-2 / scaling
    "matic-network": "L2", "polygon-ecosystem-token": "L2",
    "arbitrum": "L2", "optimism": "L2", "starknet": "L2",
    "immutable-x": "L2", "loopring": "L2", "metis-token": "L2",
    "base": "L2", "blast": "L2", "manta-network": "L2",
    # DeFi blue chips
    "uniswap": "DeFi", "aave": "DeFi", "maker": "DeFi", "compound-governance-token": "DeFi",
    "curve-dao-token": "DeFi", "lido-dao": "DeFi", "synthetix-network-token": "DeFi",
    "yearn-finance": "DeFi", "sushi": "DeFi", "raydium": "DeFi",
    "jupiter-exchange-solana": "DeFi", "pendle": "DeFi", "morpho": "DeFi",
    "ondo-finance": "DeFi", "rocket-pool": "DeFi", "frax-share": "DeFi",
    "gmx": "DeFi", "dydx-chain": "DeFi", "ethena": "DeFi", "spark": "DeFi",
    # Memes
    "dogecoin": "Meme", "shiba-inu": "Meme", "pepe": "Meme", "dogwifcoin": "Meme",
    "bonk": "Meme", "floki": "Meme", "memecoin-2": "Meme", "popcat": "Meme",
    "mog-coin": "Meme", "fartcoin": "Meme", "neiro-ethereum": "Meme",
    # AI tokens
    "render-token": "AI", "near": "AI", "fetch-ai": "AI",
    "ocean-protocol": "AI", "the-graph": "AI", "akash-network": "AI",
    "bittensor": "AI", "internet-computer": "AI", "io": "AI", "virtuals-protocol": "AI",
    "tao": "AI", "worldcoin-wld": "AI", "ai16z": "AI",
    # Stablecoins
    "tether": "Stable", "usd-coin": "Stable", "dai": "Stable",
    "ethena-usde": "Stable", "first-digital-usd": "Stable", "paypal-usd": "Stable",
    "true-usd": "Stable", "frax": "Stable", "paxos-standard": "Stable",
    "binance-usd": "Stable", "sky-dollar": "Stable", "crvusd": "Stable",
    "liquity-usd": "Stable", "gho": "Stable", "usdd": "Stable",
    # Liquid staking + restaking
    "staked-ether": "LST", "wrapped-steth": "LST", "rocket-pool-eth": "LST",
    "frax-ether": "LST", "jito-staked-sol": "LST", "marinade-staked-sol": "LST",
    "eigenpie": "Restaking", "etherfi-staked-eth": "LST", "renzo-restaked-eth": "Restaking",
    "pufeth": "Restaking",
    # Wrapped BTC
    "wrapped-bitcoin": "wBTC", "tbtc": "wBTC", "binance-bitcoin": "wBTC",
    # RWA / treasury tokens
    "ondo-finance": "RWA", "blackrock-buidl": "RWA", "mountain-protocol-usdm": "RWA",
    "ousg": "RWA",
    # Exchange tokens
    "binancecoin": "Exchange", "okb": "Exchange", "cronos": "Exchange",
    "leo-token": "Exchange", "ftx-token": "Exchange", "kucoin-shares": "Exchange",
}

SECTOR_ORDER = ["L1", "L2", "DeFi", "Meme", "AI", "Stable", "LST",
                "Restaking", "wBTC", "RWA", "Exchange", "Other"]


def sector_of(coin_id: str) -> str:
    return COIN_SECTORS.get((coin_id or "").lower(), "Other")


def group(coins: list[dict]) -> dict:
    """Group a CoinGecko universe slice by sector.

    Returns:
      {
        "by_sector": {<sector>: {count, total_mcap, total_vol_24h,
                                  avg_change_24h, top_coins[:5]}},
        "ordered": [(sector, info), ...] in SECTOR_ORDER.
      }
    """
    by_sector: dict[str, dict] = {}
    for c in coins or []:
        sec = sector_of(c.get("id", ""))
        b = by_sector.setdefault(sec, {
            "sector": sec, "count": 0, "total_mcap": 0.0,
            "total_volume_24h": 0.0, "change_24h_sum": 0.0,
            "change_24h_n": 0, "top_coins": [],
        })
        b["count"] += 1
        b["total_mcap"] += (c.get("market_cap") or 0)
        b["total_volume_24h"] += (c.get("total_volume") or 0)
        if c.get("change_24h") is not None:
            b["change_24h_sum"] += c["change_24h"]
            b["change_24h_n"] += 1
        b["top_coins"].append(c)

    out_rows = []
    for sec, b in by_sector.items():
        # Sort top coins by mcap, take top 5
        b["top_coins"].sort(key=lambda c: c.get("market_cap") or 0, reverse=True)
        b["top_coins"] = b["top_coins"][:5]
        if b["change_24h_n"]:
            b["avg_change_24h"] = round(b["change_24h_sum"] / b["change_24h_n"], 2)
        else:
            b["avg_change_24h"] = None
        b.pop("change_24h_sum", None)
        b.pop("change_24h_n", None)
        out_rows.append(b)

    # Order by SECTOR_ORDER, then by total mcap for any not in the canonical list
    def _sort_key(b):
        try:
            return (SECTOR_ORDER.index(b["sector"]), -b["total_mcap"])
        except ValueError:
            return (len(SECTOR_ORDER), -b["total_mcap"])
    out_rows.sort(key=_sort_key)
    return {
        "by_sector": {b["sector"]: b for b in out_rows},
        "ordered": out_rows,
        "sectors_count": len(out_rows),
    }
