"""Resolve a CoinGecko coin_id to its on-chain footprint.

CoinGecko's `/coins/{id}` detail response includes a `platforms` dict
mapping chain -> contract address (or empty for native coins). We use
that to fan out per-chain on-chain calls (Etherscan / Solscan).

If the coin is native (BTC / ETH / SOL / etc.), we route to the
chain-level network metrics instead of token-level.

Public entrypoint: ``per_coin_context(coin_id)`` -> a structured dict.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ingestion import coingecko, etherscan_token, solscan

# Map CoinGecko platform IDs to our internal chain names used by
# etherscan_token.CHAIN_IDS.
PLATFORM_MAP = {
    "ethereum":             "ethereum",
    "binance-smart-chain":  "bsc",
    "polygon-pos":          "polygon",
    "base":                 "base",
    "arbitrum-one":         "arbitrum",
    "optimistic-ethereum":  "optimism",
    "avalanche":            "avalanche",
}

# Coins that are native (no contract) - their on-chain context is the
# chain network itself, not a token contract.
NATIVE_COINS = {
    "bitcoin":     "bitcoin",
    "ethereum":    "ethereum",
    "solana":      "solana",
    "bnb":         "bsc",
    "binancecoin": "bsc",
    "matic-network": "polygon",
    "polygon-ecosystem-token": "polygon",
    "avalanche-2": "avalanche",
}


def per_coin_context(coin_id: str) -> dict:
    """Pull per-coin on-chain context based on CoinGecko platform metadata.

    Returns:
      {
        "coin_id": ...,
        "kind": "native" | "token" | "unknown",
        "tokens": [{"chain": ..., "contract": ..., "info": ...}, ...],
        "native_chain": "ethereum" | "bitcoin" | ...,
        "fetched_at": ...,
      }
    """
    out: dict = {
        "coin_id": coin_id,
        "kind": "unknown",
        "tokens": [],
        "native_chain": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # Native coin?
    if coin_id in NATIVE_COINS:
        out["kind"] = "native"
        out["native_chain"] = NATIVE_COINS[coin_id]
        return out

    detail = coingecko.coin_detail(coin_id)
    platforms = (detail.get("platforms") if isinstance(detail, dict) else None) or {}
    if not isinstance(platforms, dict):
        return out

    # Fan out to each known chain we have an explorer for.
    for cg_platform, contract in platforms.items():
        if not contract or not isinstance(contract, str):
            continue
        chain = PLATFORM_MAP.get(cg_platform)
        if not chain:
            continue
        info: dict
        if chain == "solana":
            info = solscan.token_meta(contract)
        else:
            info = etherscan_token.token_info(chain, contract)
        out["tokens"].append({
            "chain": chain,
            "contract": contract,
            "info": info,
        })
    if out["tokens"]:
        out["kind"] = "token"
    # Also include Solana platform from CoinGecko (uses key "solana")
    sol_addr = (platforms or {}).get("solana")
    if sol_addr and not any(t["chain"] == "solana" for t in out["tokens"]):
        out["tokens"].append({
            "chain": "solana",
            "contract": sol_addr,
            "info": solscan.token_meta(sol_addr),
        })
        out["kind"] = "token"
    return out
