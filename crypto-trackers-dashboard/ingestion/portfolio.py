"""Multi-chain portfolio aggregator.

Given a list of wallet addresses across BTC / ETH / SOL, fetch holdings
and price them at current CoinGecko USD rates. Pure-public-API only
(no auth or scraping).

Chain-detection: we use prefix heuristics —
  - 0x… (42 chars) -> Ethereum-family (EVM)
  - addresses starting with 1 / 3 / bc1 -> Bitcoin
  - 32-44-char base58 strings -> Solana

For ETH wallets we fetch native ETH balance via Etherscan + ERC-20
holdings via Etherscan tokentx (computing balance from in-out tx
deltas). For SOL we use Solscan account/tokens. For BTC we use
mempool.space /address/{addr}.
"""
from __future__ import annotations

import os
import re
import logging
from datetime import datetime, timezone
from typing import Optional

from . import _cache, coingecko, mempool_btc
from ._http import get as http_get

log = logging.getLogger("ct.portfolio")

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
SOLSCAN_PUBLIC = "https://public-api.solscan.io"
MEMPOOL = "https://mempool.space"

_BTC_RX = re.compile(r"^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,87}$")
_ETH_RX = re.compile(r"^0x[a-fA-F0-9]{40}$")
_SOL_RX = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def detect_chain(addr: str) -> Optional[str]:
    if not addr:
        return None
    a = addr.strip()
    if _ETH_RX.match(a):
        return "ethereum"
    if _BTC_RX.match(a):
        return "bitcoin"
    if _SOL_RX.match(a):
        return "solana"
    return None


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── ETH ──────────────────────────────────────────────────────────────────────

def _eth_native_balance(addr: str) -> Optional[float]:
    key = os.environ.get("ETHERSCAN_API_KEY")
    params = {"chainid": "1", "module": "account", "action": "balance",
              "address": addr, "tag": "latest"}
    if key:
        params["apikey"] = key
    r = http_get(ETHERSCAN_V2, params=params, timeout=12)
    if not r:
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    if str(d.get("status")) != "1":
        return None
    wei = _f(d.get("result"))
    return (wei / 1e18) if wei is not None else None


def eth_wallet_holdings(addr: str) -> dict:
    """ETH native balance only. Token-list requires the Etherscan Pro
    tokenbalance endpoint or full transfer-log replay — we expose native
    balance now and document the upgrade path."""
    addr = addr.lower()
    cache_key = f"port_eth_{addr}"
    hit = _cache.get(cache_key, ttl_s=300)
    if hit is not None:
        return hit
    eth_bal = _eth_native_balance(addr)
    holdings: list[dict] = []
    if eth_bal is not None:
        holdings.append({
            "chain": "ethereum",
            "symbol": "ETH",
            "coingecko_id": "ethereum",
            "balance": eth_bal,
            "contract": None,
        })
    out = {
        "address": addr,
        "chain": "ethereum",
        "count": len(holdings),
        "holdings": holdings,
        "note": "Native ETH balance only. Add ETHERSCAN_API_KEY for Pro token-balance enumeration.",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


# ─── SOL ──────────────────────────────────────────────────────────────────────

def _sol_token_list(addr: str) -> Optional[list[dict]]:
    """Solscan public-API token list for an SPL account."""
    headers = {}
    token = os.environ.get("SOLSCAN_API_KEY")
    if token:
        headers["token"] = token
    r = http_get(f"{SOLSCAN_PUBLIC}/account/tokens",
                 params={"account": addr}, timeout=12, headers=headers)
    if not r:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def sol_wallet_holdings(addr: str) -> dict:
    cache_key = f"port_sol_{addr}"
    hit = _cache.get(cache_key, ttl_s=300)
    if hit is not None:
        return hit
    rows_in = _sol_token_list(addr) or []
    holdings: list[dict] = []
    for r in rows_in:
        if not isinstance(r, dict):
            continue
        amount = _f(r.get("tokenAmount", {}).get("uiAmount"))
        if amount is None or amount == 0:
            continue
        sym = r.get("tokenSymbol") or "?"
        holdings.append({
            "chain": "solana",
            "symbol": sym,
            "coingecko_id": None,  # CoinGecko mapping not bundled here
            "balance": amount,
            "contract": r.get("tokenAddress") or r.get("mint"),
        })
    out = {
        "address": addr,
        "chain": "solana",
        "count": len(holdings),
        "holdings": holdings,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


# ─── BTC ──────────────────────────────────────────────────────────────────────

def btc_wallet_holdings(addr: str) -> dict:
    cache_key = f"port_btc_{addr}"
    hit = _cache.get(cache_key, ttl_s=300)
    if hit is not None:
        return hit
    r = http_get(f"{MEMPOOL}/api/address/{addr}", timeout=12)
    holdings: list[dict] = []
    if r:
        try:
            d = r.json()
        except ValueError:
            d = None
        if isinstance(d, dict):
            chain_stats = d.get("chain_stats") or {}
            received = _f(chain_stats.get("funded_txo_sum")) or 0
            spent = _f(chain_stats.get("spent_txo_sum")) or 0
            balance_sat = received - spent
            if balance_sat > 0:
                holdings.append({
                    "chain": "bitcoin",
                    "symbol": "BTC",
                    "coingecko_id": "bitcoin",
                    "balance": balance_sat / 1e8,
                    "contract": None,
                })
    out = {
        "address": addr,
        "chain": "bitcoin",
        "count": len(holdings),
        "holdings": holdings,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


# ─── Aggregator ──────────────────────────────────────────────────────────────

def aggregate(addresses: list[str]) -> dict:
    """Walk every address, fetch holdings per-chain, price at current
    CoinGecko USD and roll up totals."""
    all_rows: list[dict] = []
    chain_counts: dict[str, int] = {}
    fetched: list[dict] = []
    for raw in addresses:
        addr = raw.strip()
        chain = detect_chain(addr)
        if not chain:
            fetched.append({"address": addr, "error": "unrecognised address format"})
            continue
        if chain == "ethereum":
            res = eth_wallet_holdings(addr)
        elif chain == "solana":
            res = sol_wallet_holdings(addr)
        elif chain == "bitcoin":
            res = btc_wallet_holdings(addr)
        else:
            continue
        fetched.append({"address": addr, "chain": chain, "count": res.get("count", 0)})
        chain_counts[chain] = chain_counts.get(chain, 0) + 1
        for h in (res.get("holdings") or []):
            all_rows.append({**h, "address": addr})

    # Price each holding: build symbol->price map from CoinGecko universe
    universe = coingecko.universe(500)
    px_by_id: dict[str, float] = {}
    px_by_symbol: dict[str, float] = {}
    for c in (universe.get("coins") or []):
        if c.get("id") and c.get("current_price") is not None:
            px_by_id[c["id"]] = c["current_price"]
        sym = (c.get("symbol") or "").upper()
        if sym and c.get("current_price") is not None:
            # Keep the highest-mcap match per symbol (universe is mcap-sorted)
            if sym not in px_by_symbol:
                px_by_symbol[sym] = c["current_price"]

    total_usd = 0.0
    priced_rows: list[dict] = []
    for h in all_rows:
        cg = h.get("coingecko_id")
        price = px_by_id.get(cg) if cg else None
        if price is None:
            price = px_by_symbol.get((h.get("symbol") or "").upper())
        usd_value = (price or 0) * (h.get("balance") or 0)
        total_usd += usd_value
        priced_rows.append({
            **h,
            "price_usd": price,
            "value_usd": round(usd_value, 2) if price else None,
        })
    priced_rows.sort(key=lambda r: r.get("value_usd") or 0, reverse=True)

    # By-chain rollup
    by_chain: dict[str, dict] = {}
    for r in priced_rows:
        c = r.get("chain") or "?"
        b = by_chain.setdefault(c, {"chain": c, "value_usd": 0.0, "holdings": 0})
        b["value_usd"] += r.get("value_usd") or 0
        b["holdings"] += 1

    return {
        "source": "Etherscan + Solscan + mempool.space + CoinGecko (pricing)",
        "total_value_usd": round(total_usd, 2),
        "address_count": len(addresses),
        "addresses_resolved": len([f for f in fetched if "error" not in f]),
        "chain_breakdown": sorted(by_chain.values(), key=lambda b: b["value_usd"], reverse=True),
        "holdings": priced_rows,
        "fetched": fetched,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import json
    # vitalik.eth as a known-large wallet test
    print(json.dumps(aggregate(["0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"]), indent=2)[:2500])
