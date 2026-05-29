"""NSIDC Sea Ice Index G02135 v4.0 — daily Arctic + Antarctic extent (M km²)."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

URL_NORTH = "https://noaadata.apps.nsidc.org/NOAA/G02135/north/daily/data/N_seaice_extent_daily_v4.0.csv"
URL_SOUTH = "https://noaadata.apps.nsidc.org/NOAA/G02135/south/daily/data/S_seaice_extent_daily_v4.0.csv"
SOURCE = "NSIDC Sea Ice Index G02135 v4.0"
UNITS = "million km²"


def parse(text: str) -> list[dict]:
    """Parse one hemisphere's NSIDC CSV. Drops the 2 header rows."""
    series: list[dict] = []
    rdr = csv.reader(io.StringIO(text))
    rows = list(rdr)
    if len(rows) < 3:
        return []
    for row in rows[2:]:
        if len(row) < 4:
            continue
        try:
            year = int(row[0].strip())
            month = int(row[1].strip())
            day = int(row[2].strip())
            extent = float(row[3].strip())
        except ValueError:
            continue
        if extent <= 0:
            continue
        series.append({"year": year, "month": month, "day": day,
                       "extent_mkm2": round(extent, 4)})
    return series


def fetch() -> Optional[dict]:
    cached = cache.get("sea_ice")
    if cached is not None:
        return cached
    out: dict = {
        "source": SOURCE,
        "units": UNITS,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    rn = http.get(URL_NORTH, timeout=30)
    if rn:
        out["arctic"] = parse(rn.text)
    rs = http.get(URL_SOUTH, timeout=30)
    if rs:
        out["antarctic"] = parse(rs.text)
    if not out.get("arctic") and not out.get("antarctic"):
        return None
    cache.set("sea_ice", out)
    return out
