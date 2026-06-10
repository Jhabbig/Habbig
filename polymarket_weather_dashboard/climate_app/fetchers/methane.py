"""NOAA GML globally-averaged monthly methane CH₄ (ppb)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

URL = "https://gml.noaa.gov/webdata/ccgg/trends/ch4/ch4_mm_gl.csv"
SOURCE = "NOAA GML globally-averaged CH4 (ch4_mm_gl)"
UNITS = "ppb"


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
            ppb_avg = float(parts[3])
        except ValueError:
            continue
        if ppb_avg < 0:
            continue
        series.append({
            "year": year, "month": month,
            "decimal_date": round(decimal_date, 4),
            "ppb": round(ppb_avg, 2),
        })
    return series


def fetch() -> Optional[dict]:
    cached = cache.get("methane")
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
    cache.set("methane", out)
    return out
