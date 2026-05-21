"""US Drought Monitor categorical-percentage feed.

USDM publishes weekly maps + a tabular service that returns the percent of
each US area in each drought category (D0..D4, plus a "None" bucket).
Free, no key.

Endpoint (CONUS, latest week):
    https://usdmdataservices.unl.edu/api/CategoricalPercent/GetUSCategoricalPercent
        ?aoi=conus&statisticsType=1
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import _cache
from ._http import get as http_get

USDM_URL = "https://usdmdataservices.unl.edu/api/CategoricalPercent/GetUSCategoricalPercent"


def latest_categorical(aoi: str = "conus") -> dict:
    """``aoi`` can be ``"conus"`` (lower-48 + DC) or ``"total"`` (50 states + PR)."""
    cache_key = f"usdm_{aoi}"
    hit = _cache.get(cache_key, ttl_s=12 * 3600)  # weekly data, refresh twice/day
    if hit is not None:
        return hit
    params = {"aoi": aoi, "statisticsType": "1"}
    r = http_get(USDM_URL, params=params, timeout=30)
    if not r:
        return {"error": "USDM fetch failed"}
    try:
        data = r.json()
    except ValueError:
        return {"error": "USDM parse failed"}
    if not isinstance(data, list) or not data:
        return {"error": "USDM empty response"}
    # USDM returns a list of weekly snapshots; take the latest by MapDate
    latest = max(data, key=lambda d: (d.get("MapDate") or ""))

    def _pct(key: str) -> float:
        v = latest.get(key)
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return 0.0

    out = {
        "source": "US Drought Monitor (UNL CategoricalPercent service)",
        "aoi": aoi,
        "map_date": latest.get("MapDate"),
        "none_pct": _pct("None"),
        "d0_pct": _pct("D0"),
        "d1_pct": _pct("D1"),
        "d2_pct": _pct("D2"),
        "d3_pct": _pct("D3"),
        "d4_pct": _pct("D4"),
        "d2_plus_pct": round(_pct("D2") + _pct("D3") + _pct("D4"), 2),
        "d3_plus_pct": round(_pct("D3") + _pct("D4"), 2),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put(cache_key, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(latest_categorical(), indent=2))
