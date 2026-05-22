"""Rutgers Global Snow Lab — Northern Hemisphere snow cover extent.

Monthly extent in million km². Like ocean_heat.py the URL is best-effort:
Rutgers has hosted this data at a stable URL for years but we can't verify
from this environment.

Expected format (best-effort): plain-text file with rows of either
"year month extent_km2" or "year jan feb mar ... dec" (wide). The parser
tries both.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

URL = "https://climate.rutgers.edu/snowcover/files/moncov.nhland.txt"
SOURCE = "Rutgers Global Snow Lab — NH land snow cover, monthly"
UNITS = "million km²"


def parse(text: str) -> list[dict]:
    series: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            year = int(parts[0])
        except ValueError:
            continue
        if not 1960 <= year <= 2100:
            continue
        # Detect format. Long: "year month extent". Wide: "year v1 v2 ... v12".
        if len(parts) == 13:
            # Wide format with 12 monthly values
            for mi, v in enumerate(parts[1:], start=1):
                try:
                    extent = float(v)
                except ValueError:
                    continue
                # Rutgers publishes in km²; convert to million km² for axis
                # consistency with sea ice. Values >100 are clearly km².
                mkm2 = extent / 1_000_000 if extent > 1000 else extent
                if 0 < mkm2 < 60:
                    series.append({"year": year, "month": mi,
                                   "extent_mkm2": round(mkm2, 3)})
        elif len(parts) >= 3:
            # Long format: year month value
            try:
                month = int(parts[1])
                extent = float(parts[2])
            except ValueError:
                continue
            if 1 <= month <= 12:
                mkm2 = extent / 1_000_000 if extent > 1000 else extent
                if 0 < mkm2 < 60:
                    series.append({"year": year, "month": month,
                                   "extent_mkm2": round(mkm2, 3)})
    series.sort(key=lambda r: (r["year"], r["month"]))
    return series


def fetch() -> Optional[dict]:
    cached = cache.get("snow_cover")
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
        "url": URL,
        "monthly": series,
        "latest": series[-1],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache.set("snow_cover", out)
    return out
