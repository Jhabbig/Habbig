"""NOAA GML globally-averaged monthly sulfur hexafluoride SF₆ (ppt).

Same file format as CO₂/CH₄/N₂O. SF₆ is a potent greenhouse gas (GWP ~25,000
over 100 yr) with an extremely long atmospheric lifetime; concentrations rise
~0.3 ppt/yr from electrical-industry leaks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

URL = "https://gml.noaa.gov/webdata/ccgg/trends/sf6/sf6_mm_gl.csv"
SOURCE = "NOAA GML globally-averaged SF6 (sf6_mm_gl)"
UNITS = "ppt"


def parse(text: str) -> list[dict]:
    series: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            year = int(parts[0])
            month = int(parts[1])
            decimal_date = float(parts[2])
            ppt_avg = float(parts[3])
        except ValueError:
            continue
        if ppt_avg < 0:
            continue
        series.append({
            "year": year, "month": month,
            "decimal_date": round(decimal_date, 4),
            "ppt": round(ppt_avg, 3),
        })
    return series


def fetch() -> Optional[dict]:
    cached = cache.get("sf6")
    if cached is not None:
        return cached
    r = http.get(URL, timeout=30)
    if not r:
        return None
    series = parse(r.text)
    if not series:
        return None
    out = {
        "source": SOURCE,
        "units": UNITS,
        "monthly": series,
        "latest": series[-1],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache.set("sf6", out)
    return out
