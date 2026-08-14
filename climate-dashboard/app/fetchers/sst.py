"""Global SST 60°S-60°N — Climate Reanalyzer's daily-OISST JSON dump."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

URL = "https://climatereanalyzer.org/clim/sst_daily/json/oisst2.1_world2_sst_day.json"
SOURCE = "NOAA OISST v2.1 (world 60S-60N) via climatereanalyzer.org"
UNITS = "°C"


def fetch() -> Optional[dict]:
    cached = cache.get("sst")
    if cached is not None:
        return cached
    r = http.get(URL, timeout=30)
    if not r:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    out = {
        "source": SOURCE,
        "units": UNITS,
        "series": data,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache.set("sst", out)
    return out
