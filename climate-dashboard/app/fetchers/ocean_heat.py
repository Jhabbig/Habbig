"""NOAA NCEI ocean heat content (0-2000m, yearly anomaly, 10^22 J).

Ocean heat content is the integrating quantity climate scientists trust most
— atmospheric noise averages out and you see the underlying energy
accumulation. NOAA NCEI publishes seasonal and yearly CSV files at a stable
URL pattern under their oceans/woa data tree.

URL is best-effort: if NCEI restructures their data hosting we'll return
None and the dashboard's card will render an "data unavailable" placeholder
instead of disappearing.

Expected format (best-effort): CSV with a header line containing "YEAR"
and at least one column for the world (WO) anomaly, plus regional breakdowns
we ignore. Values in 10^22 J.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

URL = "https://www.ncei.noaa.gov/data/oceans/woa/DATA_ANALYSIS/3M_HEAT_CONTENT/DATA/basin/yearly/heat_content_anomaly_0-2000_yearly.csv"
SOURCE = "NOAA NCEI 0-2000m Ocean Heat Content (yearly anomaly)"
UNITS = "10^22 J"


def parse(text: str) -> list[dict]:
    """Best-effort parser. Returns a list of {year, ohc_1e22_J} dicts.

    NCEI's file has variable header conventions across products; we look for
    any line whose first token parses as an integer year in [1900, 2100],
    then take the SECOND numeric token on that line as the world anomaly.
    Tolerates whitespace- or comma-separated values.
    """
    series: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.upper().startswith("YEAR"):
            continue
        # Try comma-separated first, then whitespace-separated
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            parts = line.split()
        if len(parts) < 2:
            continue
        try:
            year = int(parts[0])
        except ValueError:
            continue
        if not 1900 <= year <= 2100:
            continue
        # First numeric value after the year column
        ohc = None
        for p in parts[1:]:
            try:
                ohc = float(p)
                break
            except ValueError:
                continue
        if ohc is None:
            continue
        series.append({"year": year, "ohc_1e22_J": round(ohc, 3)})
    return series


def fetch() -> Optional[dict]:
    cached = cache.get("ocean_heat")
    if cached is not None:
        return cached
    r = http.get(URL, timeout=30)
    if not r:
        return None
    series = parse(r.text)
    if not series:
        return None
    series.sort(key=lambda r: r["year"])
    out = {
        "source": SOURCE,
        "units": UNITS,
        "url": URL,
        "yearly": series,
        "latest": series[-1],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache.set("ocean_heat", out)
    return out
