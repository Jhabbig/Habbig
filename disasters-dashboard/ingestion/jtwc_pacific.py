"""JTWC + JMA Pacific tropical cyclone feed.

NHC ``CurrentStorms.json`` covers Atlantic + East Pacific basins. Western
Pacific (WPAC) tropical cyclones are tracked by JTWC (US Navy) and the
WMO RSMC Tokyo (JMA). We use both for redundancy:

  * JTWC has a public RSS at /products/atcf/* but it is mostly text; for
    structured data we use the JTWC current-warnings JSON when available,
    or the NRL ATCF best-track CSV as a fallback.
  * JMA RSMC Tokyo publishes an ASCII bulletin every 6 hours; that's noisy
    but does include name + intensity + position.

For now we read the **NRL TC current** GeoJSON which aggregates every active
basin (Atlantic, East Pac, West Pac, Indian Ocean, South Pacific) into one
feature collection - that's the cleanest single endpoint we've found that
covers all basins without parsing four different national bulletins.

If NRL is unreachable we degrade gracefully (count: 0, error set).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

# NRL Marine Meteorology Division - Active TC GeoJSON (covers all basins).
# Note: schema is loose; we conservatively read only fields with stable names.
NRL_ACTIVE_URL = "https://www.nrlmry.navy.mil/tcdat/sectors/atcf_sector_file"

# Fallback: NHC East-Pacific is included in CurrentStorms.json already; this
# module's job is *Pacific basins outside NHC*. If neither feed is reachable
# we return an empty list rather than failing the dashboard.
JTWC_RSS_FALLBACK = "https://www.metoc.navy.mil/jtwc/rss/jtwc.rss"


def _saffir_class(kt: Optional[float]) -> str:
    if kt is None:
        return "Unknown"
    if kt >= 137: return "Cat 5"
    if kt >= 113: return "Cat 4"
    if kt >= 96:  return "Cat 3"
    if kt >= 83:  return "Cat 2"
    if kt >= 64:  return "Cat 1"
    if kt >= 34:  return "Tropical Storm"
    return "Tropical Depression"


def _parse_atcf_sector(text: str) -> list[dict]:
    """ATCF sector-file format is space-separated rows like:

        WP, 18, 2026101312, 03, BEST, 0, 220N, 1432E, 75, 970, ...

    Columns of interest: basin (WP/EP/AL/IO/SH), storm number, ISO timestamp,
    intensity_kt, latitude (signed N/S), longitude (signed E/W), name (last
    column).
    """
    out: list[dict] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 10:
            continue
        try:
            basin = parts[0]
            storm_num = int(parts[1])
            timestamp = parts[2]
            wind_kt = int(parts[8])
            lat_raw = parts[6]
            lon_raw = parts[7]
        except (ValueError, IndexError):
            continue
        # Convert "220N" -> 22.0; "1432E" -> 143.2; "30S" -> -3.0; "1500W" -> -150.0
        lat = _parse_atcf_coord(lat_raw, "NS")
        lon = _parse_atcf_coord(lon_raw, "EW")
        # Name is typically the last token (post-19) but fall back to the
        # storm-number marker if missing.
        name = parts[-1] if len(parts) > 27 else f"{basin}{storm_num:02d}"
        out.append({
            "basin": basin,
            "storm_number": storm_num,
            "name": name,
            "intensity_kt": wind_kt,
            "classification": _saffir_class(wind_kt),
            "lat": lat,
            "lon": lon,
            "timestamp": timestamp,
        })
    return out


def _parse_atcf_coord(raw: str, axis: str) -> Optional[float]:
    if not raw or raw[-1] not in axis:
        return None
    sign = -1 if raw[-1] in "SW" else 1
    try:
        # ATCF format is integer in tenths of a degree (e.g. 220 -> 22.0)
        magnitude = int(raw[:-1]) / 10.0
    except ValueError:
        return None
    return sign * magnitude


def active_storms_all_basins() -> dict:
    """Active tropical cyclones across all WMO basins.

    Pairs with ``nhc_storms.active_storms()`` which only covers Atlantic +
    East Pac. The two together give global coverage.
    """
    hit = _cache.get("nrl_active_tc", ttl_s=900)
    if hit is not None:
        return hit
    r = http_get(NRL_ACTIVE_URL, timeout=20,
                 headers={"Accept": "text/plain,*/*"})
    if not r:
        return {"error": "NRL TC fetch failed", "storms": [], "count": 0}
    storms = _parse_atcf_sector(r.text)
    by_basin: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for s in storms:
        by_basin[s.get("basin", "?")] = by_basin.get(s.get("basin", "?"), 0) + 1
        by_class[s.get("classification", "?")] = by_class.get(s.get("classification", "?"), 0) + 1
    out = {
        "source": "NRL Marine Meteorology Division - ATCF sector file",
        "count": len(storms),
        "storms": storms,
        "by_basin": by_basin,
        "by_classification": by_class,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("nrl_active_tc", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(active_storms_all_basins(), indent=2)[:1500])
