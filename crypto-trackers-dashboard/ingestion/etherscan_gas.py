"""Etherscan + multi-chain gas tracker.

Etherscan's `/api?module=gastracker&action=gasoracle` endpoint requires a
key for sustained use but allows light unauthenticated calls. We hit it
opportunistically, plus the public ``ethgasstation``-style alternative
(blocknative or ethgastracker) when configured.

Free-tier alternative for ETH gas: https://api.etherscan.io/api with no
key returns a handful of free requests per minute - enough for a dashboard
that caches 60s.

For Polygon / BSC / Arbitrum / Base / Optimism we just pull each chain's
explorer gas-oracle endpoint with the same shape.

To keep this useful without a key we also expose ``eth_gas_blocknative``
as a separate function that uses the public blocknative gas-api endpoint
if reachable.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


ETHERSCAN_URL = "https://api.etherscan.io/api"


def eth_gas_oracle() -> dict:
    """ETH gas oracle via Etherscan. Reads ETHERSCAN_API_KEY from env if set."""
    hit = _cache.get("eth_gas", ttl_s=60)
    if hit is not None:
        return hit
    params = {"module": "gastracker", "action": "gasoracle"}
    key = os.environ.get("ETHERSCAN_API_KEY")
    if key:
        params["apikey"] = key
    r = http_get(ETHERSCAN_URL, params=params, timeout=10)
    if not r:
        return {"error": "Etherscan gas fetch failed", "chain": "ethereum"}
    try:
        d = r.json()
    except ValueError:
        return {"error": "Etherscan gas parse failed", "chain": "ethereum"}
    if str(d.get("status")) != "1" or not isinstance(d.get("result"), dict):
        return {"error": d.get("message") or "Etherscan returned no data",
                "chain": "ethereum",
                "note": "Set ETHERSCAN_API_KEY for higher rate limits."}
    res = d["result"]
    out = {
        "source": "Etherscan gas oracle",
        "chain": "ethereum",
        "safe_gwei":    _f(res.get("SafeGasPrice")),
        "propose_gwei": _f(res.get("ProposeGasPrice")),
        "fast_gwei":    _f(res.get("FastGasPrice")),
        "base_fee_gwei": _f(res.get("suggestBaseFee")),
        "block":         _f(res.get("LastBlock")),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("eth_gas", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(eth_gas_oracle(), indent=2))
