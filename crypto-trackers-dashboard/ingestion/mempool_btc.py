"""mempool.space Bitcoin network feed (free, no key).

  - /api/v1/fees/recommended  - sat/vB fee bands (fastest / 30 min / 1 h / economy)
  - /api/mempool              - mempool size + pending tx count
  - /api/v1/mining/hashrate/3d - recent hashrate snapshot
  - /api/blocks/tip/height    - current block height
  - /api/v1/difficulty-adjustment - current cycle's difficulty estimate
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

BASE = "https://mempool.space"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def network_status() -> dict:
    """Single shot: BTC fees + mempool + block height + difficulty adj."""
    hit = _cache.get("mempool_btc", ttl_s=60)
    if hit is not None:
        return hit
    fees = http_get(f"{BASE}/api/v1/fees/recommended", timeout=10)
    mem = http_get(f"{BASE}/api/mempool", timeout=10)
    tip = http_get(f"{BASE}/api/blocks/tip/height", timeout=10)
    diff = http_get(f"{BASE}/api/v1/difficulty-adjustment", timeout=10)

    out = {
        "source": "mempool.space",
        "fees_sat_per_vb": None,
        "mempool": None,
        "block_height": None,
        "difficulty_adjustment": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    if fees:
        try:
            j = fees.json()
            out["fees_sat_per_vb"] = {
                "fastest": j.get("fastestFee"),
                "30min":   j.get("halfHourFee"),
                "60min":   j.get("hourFee"),
                "economy": j.get("economyFee"),
                "minimum": j.get("minimumFee"),
            }
        except ValueError:
            pass
    if mem:
        try:
            j = mem.json()
            out["mempool"] = {
                "count": j.get("count"),
                "vsize": j.get("vsize"),
                "total_fee_sats": j.get("total_fee"),
            }
        except ValueError:
            pass
    if tip:
        try:
            out["block_height"] = int(tip.text)
        except (ValueError, TypeError):
            pass
    if diff:
        try:
            j = diff.json()
            out["difficulty_adjustment"] = {
                "progress_pct": _f(j.get("progressPercent")),
                "difficulty_change_pct": _f(j.get("difficultyChange")),
                "estimated_retarget_iso": j.get("estimatedRetargetDate"),
                "remaining_blocks": j.get("remainingBlocks"),
                "remaining_time_ms": j.get("remainingTime"),
            }
        except ValueError:
            pass
    _cache.put("mempool_btc", out)
    return out


def recent_hashrate() -> dict:
    """7-day hashrate + difficulty series."""
    hit = _cache.get("mempool_hashrate", ttl_s=3600)
    if hit is not None:
        return hit
    r = http_get(f"{BASE}/api/v1/mining/hashrate/3d", timeout=15)
    if not r:
        return {"error": "Hashrate fetch failed"}
    try:
        j = r.json()
    except ValueError:
        return {"error": "Hashrate parse failed"}
    rows = []
    for h in (j.get("hashrates") or []):
        if not isinstance(h, dict):
            continue
        rows.append({
            "ts": h.get("timestamp"),
            "hashrate_hps": h.get("avgHashrate"),
        })
    out = {
        "source": "mempool.space /api/v1/mining/hashrate/3d",
        "current_hashrate_hps": _f(j.get("currentHashrate")),
        "current_difficulty": _f(j.get("currentDifficulty")),
        "series": rows[-90:],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("mempool_hashrate", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(network_status(), indent=2))
    print(json.dumps(recent_hashrate(), indent=2)[:800])
