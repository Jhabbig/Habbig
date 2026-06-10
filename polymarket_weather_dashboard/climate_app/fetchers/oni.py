"""NOAA CPC Oceanic Niño Index (ONI) — monthly 3-month running mean of SST anomaly."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

URL = "https://psl.noaa.gov/data/correlation/oni.data"
SOURCE = "NOAA CPC Oceanic Niño Index (ONI, 3-month running)"


def parse(text: str) -> list[dict]:
    series: list[dict] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) != 13:
            continue
        try:
            year = int(parts[0])
            vals = [float(p) for p in parts[1:]]
        except ValueError:
            continue
        if not (1900 <= year <= 2100):
            continue
        for mi, v in enumerate(vals, start=1):
            if v <= -99:
                continue
            series.append({"year": year, "month": mi, "oni": round(v, 2)})
    return series


def state_for(oni_value: float) -> str:
    if oni_value >= 0.5:
        return "El Niño"
    if oni_value <= -0.5:
        return "La Niña"
    return "Neutral"


def fetch() -> Optional[dict]:
    cached = cache.get("oni")
    if cached is not None:
        return cached
    r = http.get(URL, timeout=30)
    if not r:
        return None
    series = parse(r.text)
    if not series:
        return None
    latest = series[-1]
    out = {
        "source": SOURCE,
        "monthly": series,
        "latest": latest,
        "state": state_for(latest["oni"]),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache.set("oni", out)
    return out
