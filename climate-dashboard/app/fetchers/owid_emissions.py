"""Our World in Data country-level CO₂ emissions.

OWID maintains a comprehensive CSV at github.com/owid/co2-data with country
× year × emission-metric rows going back to 1750. We pull a handful of
columns (country, ISO, year, total CO₂ emissions in million tonnes, per
capita, share of global) and bucket by ISO code.

The file is ~3 MB. We cache aggressively (24h TTL) — country emissions
update annually, not minute-by-minute.
"""
from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

URL = "https://raw.githubusercontent.com/owid/co2-data/master/owid-co2-data.csv"
SOURCE = "Our World in Data — co2-data"


def _f(s: str) -> Optional[float]:
    if s is None or s == "":
        return None
    try:
        v = float(s)
        return None if math.isnan(v) else v
    except ValueError:
        return None


def parse(text: str) -> dict:
    """Parse the OWID CSV. Returns {countries, latest_year, world_key}."""
    rdr = csv.DictReader(io.StringIO(text))
    countries: dict[str, dict] = {}
    latest_year = 0
    world_key: Optional[str] = None
    for row in rdr:
        iso = (row.get("iso_code") or "").strip()
        country = (row.get("country") or "").strip()
        try:
            year = int(row.get("year") or "")
        except ValueError:
            continue
        co2 = _f(row.get("co2"))
        if co2 is None:
            continue
        latest_year = max(latest_year, year)
        key = iso or f"__nocode_{country}"
        bucket = countries.setdefault(key,
                                       {"name": country, "iso": iso, "data": {}})
        bucket["data"][year] = {
            "co2_mt": round(co2, 2),
            "co2_per_capita_t": _f(row.get("co2_per_capita")),
            "share_global": _f(row.get("share_global_co2")),
            "gdp": _f(row.get("gdp")),
            "population": _f(row.get("population")),
        }
        # World may have iso "OWID_WRL" in some versions of the CSV, or empty
        # iso with country="World" in others. Track whichever we see.
        if country == "World" or iso == "OWID_WRL":
            world_key = key
    return {
        "countries": countries,
        "latest_year": latest_year,
        "world_key": world_key,
    }


def fetch() -> Optional[dict]:
    cached = cache.get("owid_emissions")
    if cached is not None:
        return cached
    r = http.get(URL, timeout=60)  # larger file → bigger timeout
    if not r:
        return None
    parsed = parse(r.text)
    if not parsed.get("countries"):
        return None
    out = {
        "source": SOURCE,
        "url": URL,
        **parsed,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache.set("owid_emissions", out)
    return out
