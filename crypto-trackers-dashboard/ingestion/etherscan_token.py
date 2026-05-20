"""Etherscan ERC-20 token-level context.

Two free endpoints we use:
  - module=token&action=tokeninfo - holder count, total supply, contract age
  - module=stats&action=tokensupply - circulating supply
  - module=account&action=tokentx - recent transfer history

These all benefit from an ETHERSCAN_API_KEY (set in env) but work for
occasional polls without one. Tokeninfo specifically requires a paid Etherscan
plan; we degrade gracefully when it returns a non-1 status.

Also exposes a generic chain-explorer fan-out: same Etherscan-API-V2-compatible
endpoints work for BSCScan, Polygonscan, Basescan, Arbiscan, Optimistic
Etherscan via per-chain hosts.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

# Etherscan v2 multichain API: one host, chain selected via chainid param
# https://api.etherscan.io/v2/api?chainid=1&module=...
ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"

CHAIN_IDS = {
    "ethereum": 1,
    "bsc":      56,
    "polygon":  137,
    "base":     8453,
    "arbitrum": 42161,
    "optimism": 10,
    "avalanche": 43114,
}


def _params(chain: str, **extra) -> dict:
    p = {"chainid": str(CHAIN_IDS.get(chain, 1))}
    p.update(extra)
    key = os.environ.get("ETHERSCAN_API_KEY")
    if key:
        p["apikey"] = key
    return p


def token_info(chain: str, contract: str) -> dict:
    """Token meta + holder count via Etherscan tokeninfo (Pro endpoint).

    Falls back to a minimal response when the unauthenticated tier is used.
    """
    chain = chain.lower()
    if chain not in CHAIN_IDS:
        return {"error": f"unsupported chain {chain}", "chain": chain}
    cache_key = f"tokeninfo_{chain}_{contract.lower()}"
    hit = _cache.get(cache_key, ttl_s=1800)
    if hit is not None:
        return hit
    r = http_get(ETHERSCAN_V2, params=_params(chain, module="token",
                                              action="tokeninfo",
                                              contractaddress=contract),
                 timeout=12)
    if not r:
        return {"error": "Etherscan tokeninfo fetch failed", "chain": chain,
                "contract": contract}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Etherscan tokeninfo parse failed", "chain": chain}
    if str(d.get("status")) != "1":
        return {"error": d.get("message") or "Etherscan returned no data",
                "chain": chain, "contract": contract,
                "note": "tokeninfo requires Etherscan Pro; set ETHERSCAN_API_KEY."}
    res = (d.get("result") or [None])[0]
    if not isinstance(res, dict):
        return {"error": "Etherscan returned no result", "chain": chain}
    out = {
        "source": f"Etherscan V2 tokeninfo (chain={chain})",
        "chain": chain,
        "contract": contract,
        "name": res.get("tokenName"),
        "symbol": res.get("symbol"),
        "decimals": res.get("divisor"),
        "type": res.get("tokenType"),
        "total_supply": res.get("totalSupply"),
        "circulating_supply": res.get("circulatingSupply"),
        "holder_count": res.get("holders"),
        "website": res.get("website"),
        "twitter": res.get("twitter"),
        "icon_url": res.get("tokenLogo"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def supply(chain: str, contract: str) -> dict:
    """Free-tier token supply via stats/tokensupply (works without paid key)."""
    chain = chain.lower()
    if chain not in CHAIN_IDS:
        return {"error": f"unsupported chain {chain}", "chain": chain}
    cache_key = f"supply_{chain}_{contract.lower()}"
    hit = _cache.get(cache_key, ttl_s=900)
    if hit is not None:
        return hit
    r = http_get(ETHERSCAN_V2, params=_params(chain, module="stats",
                                              action="tokensupply",
                                              contractaddress=contract),
                 timeout=12)
    if not r:
        return {"error": "Etherscan supply fetch failed", "chain": chain}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Etherscan supply parse failed", "chain": chain}
    if str(d.get("status")) != "1":
        return {"error": d.get("message") or "Etherscan supply returned no data",
                "chain": chain, "contract": contract}
    out = {
        "source": f"Etherscan V2 tokensupply (chain={chain})",
        "chain": chain,
        "contract": contract,
        "raw_supply": d.get("result"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


def gas_oracle(chain: str) -> dict:
    """Gas-tracker for any supported chain via Etherscan V2."""
    chain = chain.lower()
    if chain not in CHAIN_IDS:
        return {"error": f"unsupported chain {chain}", "chain": chain}
    cache_key = f"gas_{chain}"
    hit = _cache.get(cache_key, ttl_s=30)
    if hit is not None:
        return hit
    r = http_get(ETHERSCAN_V2, params=_params(chain, module="gastracker",
                                              action="gasoracle"), timeout=10)
    if not r:
        return {"error": "Etherscan gas fetch failed", "chain": chain}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Etherscan gas parse failed", "chain": chain}
    if str(d.get("status")) != "1":
        return {"error": d.get("message") or "Etherscan gas returned no data",
                "chain": chain}
    res = d.get("result") or {}

    def _f(v) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    out = {
        "source": f"Etherscan V2 gas oracle (chain={chain})",
        "chain": chain,
        "safe_gwei": _f(res.get("SafeGasPrice")),
        "propose_gwei": _f(res.get("ProposeGasPrice")),
        "fast_gwei": _f(res.get("FastGasPrice")),
        "base_fee_gwei": _f(res.get("suggestBaseFee")),
        "last_block": _f(res.get("LastBlock")),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(token_info("ethereum",
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"), indent=2))  # WETH
    print(json.dumps(gas_oracle("polygon"), indent=2))
