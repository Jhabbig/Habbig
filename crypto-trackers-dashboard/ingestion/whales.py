"""Whale-transaction tracker.

Tracks two complementary signals:

  1. **Large ETH transfers**: poll Etherscan's free public account-balance
     endpoint to monitor the top N exchange hot/cold wallets and detect
     significant balance changes (>1000 ETH in/out per refresh) as a
     coarse exchange-flow proxy. Requires no key for occasional polls.

  2. **Large BTC transfers**: poll mempool.space for the largest recent
     unconfirmed transactions (sat-value sorted) - these are typically
     whale-sized OTC settlements + exchange moves.

This is a strict superset of what's possible without a paid Whale-Alert
key. For 7-figure on-chain moves it covers ETH + BTC; for SOL/BNB/AVAX
we'd need per-chain explorers (deferred to v0.4).

Both feeds are free and require no API key, with Etherscan accepting an
optional ETHERSCAN_API_KEY for higher rate limits.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

ETHERSCAN_URL = "https://api.etherscan.io/api"
MEMPOOL_URL = "https://mempool.space"

# Top known exchange hot/cold wallets (Etherscan-labelled, 2025 snapshot).
# Used for exchange-flow tracking. Source: Etherscan label list + public
# Twitter analytics. These addresses move ETH at scale; their balance
# deltas are a leading indicator for exchange-side liquidity.
TRACKED_EXCHANGE_WALLETS = [
    ("Binance Hot Wallet 14",  "0x28C6c06298d514Db089934071355E5743bf21d60"),
    ("Binance Hot Wallet 20",  "0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549"),
    ("Binance Cold Wallet 1",  "0xF977814e90dA44bFA03b6295A0616a897441aceC"),
    ("Coinbase 1",             "0x71660c4005BA85c37ccec55d0C4493E66Fe775d3"),
    ("Coinbase 2",             "0xA9D1e08C7793af67e9d92fe308d5697FB81d3E43"),
    ("Coinbase Custody",       "0x3cD751E6b0078Be393132286c442345e5DC49699"),
    ("Kraken 4",               "0xfa52274DD61E1643d2205169732f29114BC240b3"),
    ("Kraken Cold",            "0x53d284357ec70cE289D6D64134DfAc8E511c8a3D"),
    ("OKX Hot 1",              "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b"),
    ("OKX Hot 2",              "0x236F233dBf78341d25fB0F1bD14cb2bA4b8a777e"),
    ("Bitfinex Hot",           "0x876EabF441B2EE5B5b0554Fd502a8E0600950cFa"),
    ("Bitfinex Cold",          "0xfBb1b73c4f0BDa4f67dcA266ce6Ef42f520fBB98"),
    ("Bybit Hot",              "0xf89d7b9c864f589bbF53a82105107622B35EaA40"),
    ("Robinhood",              "0x40B38765696e3d5d8d9d834D8AaD4bB6e418E489"),
]


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _wei_to_eth(wei: str) -> Optional[float]:
    try:
        return int(wei) / 1e18
    except (TypeError, ValueError):
        return None


def exchange_balances_eth() -> dict:
    """Pull current ETH balance for every tracked exchange wallet.

    Uses Etherscan's free balancemulti endpoint (up to 20 addresses per call).
    Compare with the previous cached snapshot to surface flow deltas.
    """
    hit = _cache.get("eth_exchange_balances", ttl_s=300)  # 5 min
    addresses = ",".join(a for _, a in TRACKED_EXCHANGE_WALLETS)
    params = {"module": "account", "action": "balancemulti",
              "address": addresses, "tag": "latest"}
    key = os.environ.get("ETHERSCAN_API_KEY")
    if key:
        params["apikey"] = key
    r = http_get(ETHERSCAN_URL, params=params, timeout=15)
    if not r:
        return hit or {"error": "Etherscan balance fetch failed", "wallets": []}
    try:
        d = r.json()
    except ValueError:
        return hit or {"error": "Etherscan balance parse failed", "wallets": []}
    if str(d.get("status")) != "1" or not isinstance(d.get("result"), list):
        return hit or {"error": d.get("message") or "Etherscan empty result",
                       "wallets": [],
                       "note": "Set ETHERSCAN_API_KEY for higher rate limits."}

    by_address = {row.get("account", "").lower(): _wei_to_eth(row.get("balance", "0"))
                  for row in d["result"]}
    label_by_addr = {a.lower(): label for label, a in TRACKED_EXCHANGE_WALLETS}

    # Compare to previous snapshot if we have one
    previous = hit or {}
    prev_by_addr = {w["address"].lower(): w["balance_eth"]
                    for w in (previous.get("wallets") or [])} if previous else {}

    wallets: list[dict] = []
    total_inflow = 0.0
    total_outflow = 0.0
    for addr_lower, bal in by_address.items():
        if bal is None:
            continue
        prev = prev_by_addr.get(addr_lower)
        delta = (bal - prev) if prev is not None else None
        if delta is not None:
            if delta > 0:
                total_inflow += delta
            else:
                total_outflow += -delta
        wallets.append({
            "label": label_by_addr.get(addr_lower, "?"),
            "address": addr_lower,
            "balance_eth": round(bal, 2),
            "delta_eth_since_last_poll": round(delta, 2) if delta is not None else None,
        })
    wallets.sort(key=lambda w: w["balance_eth"], reverse=True)

    # Recent significant moves (>= 500 ETH in either direction)
    significant = [w for w in wallets if w.get("delta_eth_since_last_poll") is not None
                   and abs(w["delta_eth_since_last_poll"]) >= 500]
    significant.sort(key=lambda w: abs(w["delta_eth_since_last_poll"]), reverse=True)

    out = {
        "source": "Etherscan balancemulti (exchange hot/cold wallets)",
        "wallets_tracked": len(TRACKED_EXCHANGE_WALLETS),
        "wallets": wallets,
        "total_balance_eth": round(sum(w["balance_eth"] for w in wallets), 2),
        "total_inflow_since_last_eth": round(total_inflow, 2),
        "total_outflow_since_last_eth": round(total_outflow, 2),
        "net_flow_eth": round(total_inflow - total_outflow, 2),
        "significant_moves": significant[:8],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("eth_exchange_balances", out)
    return out


def large_btc_transactions(min_btc: float = 100.0) -> dict:
    """Pull the latest mempool transactions, filter to whale-sized ones.

    mempool.space's /api/mempool/recent endpoint returns the last ~5000
    unconfirmed transactions with sat values. We filter to those that move
    >= `min_btc` BTC and return the top 25 by value.
    """
    hit = _cache.get(f"btc_whale_{min_btc}", ttl_s=120)
    if hit is not None:
        return hit
    r = http_get(f"{MEMPOOL_URL}/api/mempool/recent", timeout=15)
    if not r:
        return {"error": "mempool.space recent-tx fetch failed", "rows": []}
    try:
        rows = r.json()
    except ValueError:
        return {"error": "mempool.space recent-tx parse failed", "rows": []}
    parsed: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sat = row.get("value")
        if sat is None:
            continue
        btc = sat / 1e8
        if btc < min_btc:
            continue
        parsed.append({
            "txid": row.get("txid"),
            "value_btc": round(btc, 4),
            "fee_sats": row.get("fee"),
            "vsize_vb": row.get("vsize"),
            "time_seen_ms": row.get("firstSeen", 0) * 1000 if row.get("firstSeen") else None,
        })
    parsed.sort(key=lambda r: r["value_btc"], reverse=True)
    parsed = parsed[:25]
    out = {
        "source": "mempool.space /api/mempool/recent",
        "min_btc_threshold": min_btc,
        "count": len(parsed),
        "rows": parsed,
        "total_btc": round(sum(r["value_btc"] for r in parsed), 2),
        "biggest_btc": parsed[0]["value_btc"] if parsed else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(f"btc_whale_{min_btc}", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(exchange_balances_eth(), indent=2)[:2000])
    print(json.dumps(large_btc_transactions(50), indent=2)[:1500])
