"""NASA GISTEMP v4 — global land+ocean temperature anomaly vs 1951-1980 (°C)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

URL = "https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.csv"
SOURCE = "NASA GISTEMP v4 (GLB.Ts+dSST)"
BASELINE = "1951-1980"
UNITS = "°C"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def parse(text: str) -> Optional[dict]:
    """Parse the GISTEMP CSV. Returns None if the header row can't be found."""
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("Year,Jan"):
            header_idx = i
            break
    if header_idx is None:
        return None

    monthly: list[dict] = []
    annual: list[dict] = []
    for line in lines[header_idx + 1:]:
        parts = line.split(",")
        if len(parts) < 14:
            continue
        try:
            year = int(parts[0])
        except ValueError:
            continue
        for mi in range(1, 13):
            v = parts[mi].strip()
            if not v or v == "***":
                continue
            try:
                anomaly = float(v)
            except ValueError:
                continue
            monthly.append({"year": year, "month": mi, "anomaly_c": round(anomaly, 3)})
        try:
            ann = parts[13].strip()
            if ann and ann != "***":
                annual.append({"year": year, "anomaly_c": round(float(ann), 3)})
        except (ValueError, IndexError):
            pass
    return {"monthly": monthly, "annual": annual}


def fetch() -> Optional[dict]:
    cached = cache.get("gistemp")
    if cached is not None:
        return cached
    r = http.get(URL, timeout=30)
    if not r:
        return None
    parsed = parse(r.text)
    if not parsed or not parsed["monthly"]:
        return None
    out = {
        "source": SOURCE,
        "baseline": BASELINE,
        "units": UNITS,
        "monthly": parsed["monthly"],
        "annual": parsed["annual"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache.set("gistemp", out)
    return out
