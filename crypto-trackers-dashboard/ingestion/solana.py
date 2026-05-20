"""Solana network metrics via public RPC.

Free public RPC endpoint: https://api.mainnet-beta.solana.com
Rate-limited but works without a key for low-cadence calls.

We pull:
  - getSlot                            current slot height
  - getEpochInfo                       epoch + slot index + remaining slots
  - getRecentPerformanceSamples        last N samples for TPS computation
  - getRecentPrioritizationFees        priority-fee market signal

Cache aggressively (15-60s) since the public RPC throttles hot loops.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from . import _cache, _health

RPC_URL = "https://api.mainnet-beta.solana.com"
SOURCE = RPC_URL


def _rpc_call(method: str, params: Optional[list] = None, timeout: int = 12) -> Optional[Any]:
    import time
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    started = time.time()
    try:
        r = requests.post(RPC_URL, json=body, timeout=timeout,
                          headers={"Content-Type": "application/json",
                                   "User-Agent": "narve-crypto-trackers/1.0"})
    except requests.RequestException:
        _health.record_call(SOURCE, ok=False, latency_s=time.time() - started)
        return None
    latency = time.time() - started
    if r.status_code != 200:
        _health.record_call(SOURCE, ok=False, latency_s=latency, http_status=r.status_code)
        return None
    try:
        j = r.json()
    except (ValueError, json.JSONDecodeError):
        _health.record_call(SOURCE, ok=False, latency_s=latency, http_status=200)
        return None
    if "error" in j:
        _health.record_call(SOURCE, ok=False, latency_s=latency, http_status=200)
        return None
    _health.record_call(SOURCE, ok=True, latency_s=latency, http_status=200)
    return j.get("result")


def network_status() -> dict:
    hit = _cache.get("sol_network", ttl_s=30)
    if hit is not None:
        return hit
    slot = _rpc_call("getSlot")
    epoch_info = _rpc_call("getEpochInfo")
    perf_samples = _rpc_call("getRecentPerformanceSamples", [10])
    out: dict = {
        "source": "Solana JSON-RPC mainnet-beta",
        "slot": slot,
        "epoch_info": epoch_info,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(perf_samples, list) and perf_samples:
        tps_vals: list[float] = []
        for s in perf_samples:
            if not isinstance(s, dict):
                continue
            n_tx = s.get("numTransactions") or 0
            n_slots = s.get("numSlots") or 1
            period = s.get("samplePeriodSecs") or 1
            tps = n_tx / max(period, 1)
            tps_vals.append(tps)
        if tps_vals:
            out["tps_recent_avg"] = round(sum(tps_vals) / len(tps_vals), 1)
            out["tps_recent_peak"] = round(max(tps_vals), 1)
    if isinstance(epoch_info, dict):
        epoch = epoch_info.get("epoch")
        slot_index = epoch_info.get("slotIndex")
        slots_in_epoch = epoch_info.get("slotsInEpoch") or 432000
        progress = (slot_index or 0) / max(slots_in_epoch, 1) * 100
        out["epoch"] = epoch
        out["epoch_progress_pct"] = round(progress, 2)
        out["slots_remaining"] = max((slots_in_epoch or 0) - (slot_index or 0), 0)
    _cache.put("sol_network", out)
    return out


def priority_fees() -> dict:
    hit = _cache.get("sol_priority_fees", ttl_s=30)
    if hit is not None:
        return hit
    res = _rpc_call("getRecentPrioritizationFees", [])
    if not isinstance(res, list):
        return {"error": "Solana priority-fee fetch failed"}
    fees = sorted([r.get("prioritizationFee", 0) for r in res
                   if isinstance(r, dict)])
    if not fees:
        return {"error": "no priority-fee samples"}
    n = len(fees)
    median = fees[n // 2] if n % 2 else (fees[n // 2 - 1] + fees[n // 2]) / 2.0
    p90 = fees[min(int(n * 0.9), n - 1)]
    p99 = fees[min(int(n * 0.99), n - 1)]
    out = {
        "source": "Solana getRecentPrioritizationFees",
        "samples": n,
        "median_lamports": int(median),
        "p90_lamports": int(p90),
        "p99_lamports": int(p99),
        "max_lamports": int(fees[-1]),
        "min_lamports": int(fees[0]),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("sol_priority_fees", out)
    return out


def validator_summary() -> dict:
    """Validator-set summary via getVoteAccounts.

    Returns the full active validator list (~1500 entries) summarised:
    total active stake, count, top-N by stake with their commission,
    and nakamoto-coefficient-style "min validators needed to control
    >33% / >50% / >66% of stake" indicators.
    """
    hit = _cache.get("sol_validators", ttl_s=3600)  # 1 h — stake moves slowly
    if hit is not None:
        return hit
    res = _rpc_call("getVoteAccounts", timeout=30)
    if not isinstance(res, dict):
        return {"error": "Solana getVoteAccounts failed"}
    active = res.get("current") or []
    delinquent = res.get("delinquent") or []
    rows = []
    total_stake = 0
    for v in active:
        if not isinstance(v, dict):
            continue
        stake = v.get("activatedStake") or 0
        total_stake += stake
        rows.append({
            "vote_pubkey": v.get("votePubkey"),
            "identity": v.get("nodePubkey"),
            "active_stake_lamports": stake,
            "active_stake_sol": stake / 1e9,
            "commission": v.get("commission"),
            "last_vote": v.get("lastVote"),
            "root_slot": v.get("rootSlot"),
        })
    rows.sort(key=lambda r: r.get("active_stake_lamports") or 0, reverse=True)
    # Nakamoto-style takeover thresholds
    nakamoto = {"33pct": 0, "50pct": 0, "66pct": 0}
    cum = 0
    for i, r in enumerate(rows, start=1):
        cum += r["active_stake_lamports"]
        share = cum / max(total_stake, 1)
        if not nakamoto["33pct"] and share > 0.33:
            nakamoto["33pct"] = i
        if not nakamoto["50pct"] and share > 0.50:
            nakamoto["50pct"] = i
        if not nakamoto["66pct"] and share > 0.66:
            nakamoto["66pct"] = i
        if all(nakamoto.values()):
            break
    out = {
        "source": "Solana getVoteAccounts",
        "active_count": len(rows),
        "delinquent_count": len(delinquent),
        "total_active_stake_sol": total_stake / 1e9,
        "nakamoto_coefficients": nakamoto,
        "top_validators": rows[:25],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("sol_validators", out)
    return out


if __name__ == "__main__":
    print(json.dumps(network_status(), indent=2))
    print(json.dumps(priority_fees(), indent=2))
    print(json.dumps(validator_summary(), indent=2)[:1500])
