"""NOAA STAR Laboratory for Satellite Altimetry — global mean sea level.

GMSL rise from satellite altimetry. URL is best-effort; if NESDIS
restructures, the dashboard's card degrades to an explicit
"data unavailable" state rather than disappearing.

Expected format (best-effort): CSV with at least a date or year column
plus a sea level value in mm. Multiple plausible column names; we sniff.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

URL = "https://www.star.nesdis.noaa.gov/socd/lsa/SeaLevelRise/LSA_SLR_timeseries_global.csv"
SOURCE = "NOAA STAR Laboratory for Satellite Altimetry — global mean sea level"
UNITS = "mm"


def parse(text: str) -> list[dict]:
    """Best-effort: try CSV parse, look for any (date|year|time)+(value) pair."""
    series: list[dict] = []
    rdr = csv.reader(io.StringIO(text))
    rows = list(rdr)
    if not rows:
        return []

    # Find header row — first row with non-numeric content. If everything is
    # numeric, there's no header and we should start parsing from row 0.
    header_idx = -1
    for i, row in enumerate(rows):
        if any(not _looks_numeric(c) for c in row):
            header_idx = i
            break

    if header_idx >= 0:
        header = [h.strip().lower() for h in rows[header_idx]]
        date_col = next((i for i, h in enumerate(header) if h in ("date", "year", "time", "decimal_year", "decimal year")), None)
        value_col = next((i for i, h in enumerate(header) if "level" in h or "gmsl" in h or "msl" in h), None)
        if date_col is None or value_col is None:
            date_col, value_col = 0, 1
    else:
        # No header at all — assume first two columns are (date, value)
        date_col, value_col = 0, 1

    for row in rows[header_idx + 1:]:
        if len(row) <= max(date_col, value_col):
            continue
        d_raw = row[date_col].strip()
        v_raw = row[value_col].strip()
        try:
            decimal_year = float(d_raw)
            value_mm = float(v_raw)
        except ValueError:
            continue
        if not 1990 <= decimal_year <= 2100:
            continue
        series.append({"decimal_year": round(decimal_year, 4),
                       "sea_level_mm": round(value_mm, 2)})
    return series


def _looks_numeric(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def fetch() -> Optional[dict]:
    cached = cache.get("sea_level")
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
        "series": series,
        "latest": series[-1],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache.set("sea_level", out)
    return out
