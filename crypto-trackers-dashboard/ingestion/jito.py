"""Jito-Labs MEV tip-floor tracker.

Jito's MEV-Boost-style auction system collects tips from searchers; the
public endpoint exposes the recent tip-floor percentiles so traders /
searchers can size their tips appropriately.

  GET https://bundles.jito.wtf/api/v1/bundles/tip_floor

Free, no auth. Returns the latest 8 epochs of tip floor samples.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

TIP_FLOOR_URL = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tip_floor() -> dict:
    hit = _cache.get("jito_tip_floor", ttl_s=60)
    if hit is not None:
        return hit
    r = http_get(TIP_FLOOR_URL, timeout=12)
    if not r:
        return {"error": "Jito tip-floor fetch failed", "samples": []}
    try:
        data = r.json()
    except ValueError:
        return {"error": "Jito tip-floor parse failed", "samples": []}
    rows: list[dict] = []
    for sample in (data if isinstance(data, list) else []):
        if not isinstance(sample, dict):
            continue
        rows.append({
            "time": sample.get("time"),
            "landed_tips_25th_percentile_sol":      _f(sample.get("landed_tips_25th_percentile")),
            "landed_tips_50th_percentile_sol":      _f(sample.get("landed_tips_50th_percentile")),
            "landed_tips_75th_percentile_sol":      _f(sample.get("landed_tips_75th_percentile")),
            "landed_tips_95th_percentile_sol":      _f(sample.get("landed_tips_95th_percentile")),
            "landed_tips_99th_percentile_sol":      _f(sample.get("landed_tips_99th_percentile")),
            "ema_landed_tips_50th_percentile_sol":  _f(sample.get("ema_landed_tips_50th_percentile")),
        })
    latest = rows[-1] if rows else None
    out = {
        "source": "Jito /api/v1/bundles/tip_floor",
        "samples": len(rows),
        "history": rows,
        "latest": latest,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("jito_tip_floor", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(tip_floor(), indent=2))
