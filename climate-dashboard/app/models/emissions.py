"""Derived summaries on top of the OWID country-emissions dataset."""
from __future__ import annotations

from typing import Optional


# OWID puts continents and political groupings in the same CSV. Real
# countries have ISO-3166-1 alpha-3 codes (3 letters, no OWID_ prefix);
# aggregates either prefix with "OWID_" or omit the iso_code entirely.
def _is_country(iso: str) -> bool:
    return bool(iso) and len(iso) == 3 and not iso.startswith("OWID_")


def top_emitters(parsed: Optional[dict], *, n: int = 10, year: Optional[int] = None) -> list[dict]:
    if not parsed or not parsed.get("countries"):
        return []
    year = year or parsed.get("latest_year")
    rows: list[dict] = []
    for iso, c in parsed["countries"].items():
        if not _is_country(iso):
            continue
        d = c["data"].get(year)
        if not d:
            continue
        rows.append({
            "iso": iso,
            "country": c["name"],
            "year": year,
            "co2_mt": d["co2_mt"],
            "co2_per_capita_t": d["co2_per_capita_t"],
            "share_global": d["share_global"],
        })
    rows.sort(key=lambda r: r["co2_mt"], reverse=True)
    return rows[:n]


def global_summary(parsed: Optional[dict]) -> Optional[dict]:
    """World CO₂ emissions for the latest year + 10-year-ago comparison.

    OWID's CSV may key the World row under iso_code "OWID_WRL" (older
    versions) or with an empty iso_code and country="World" (current as of
    late 2025). The parser stashes whichever bucket it found in
    ``parsed["world_key"]``; we use that.
    """
    if not parsed or not parsed.get("countries"):
        return None
    world_key = parsed.get("world_key")
    world = parsed["countries"].get(world_key) if world_key else None
    # Fallback for older parsed dicts that didn't include world_key
    if not world:
        world = parsed["countries"].get("OWID_WRL") or parsed["countries"].get("__nocode_World")
    if not world or not world.get("data"):
        return None
    years = sorted(world["data"].keys())
    if not years:
        return None
    latest = years[-1]
    latest_co2 = world["data"][latest]["co2_mt"]
    ten_yrs_ago = latest - 10
    decade_co2 = world["data"].get(ten_yrs_ago, {}).get("co2_mt")
    return {
        "year": latest,
        "global_co2_mt": latest_co2,
        "decade_ago_year": ten_yrs_ago,
        "decade_ago_co2_mt": decade_co2,
        "decade_change_pct": (
            round((latest_co2 - decade_co2) / decade_co2 * 100, 1)
            if decade_co2 else None
        ),
    }
