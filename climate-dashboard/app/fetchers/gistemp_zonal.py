"""NASA GISTEMP zonal annual temperature — Arctic, Tropics, Antarctic, etc.

Sister file to GLB.Ts+dSST.csv. Same URL directory, wide CSV with one row
per year and one column per latitude band. Used to surface the canonical
"Arctic is warming N× faster than the global mean" framing.

URL is best-effort but high-confidence — it's the standard companion file
to the global series, hosted in the same NASA GISS directory.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

URL = "https://data.giss.nasa.gov/gistemp/tabledata_v4/ZonAnn.Ts+dSST.csv"
SOURCE = "NASA GISTEMP v4 zonal annual (ZonAnn.Ts+dSST)"

# Anchor regions we surface. Many bands are in the file; we pick the
# climate-storytelling ones:
#  - Glob          global mean
#  - NHem / SHem   hemispheric means
#  - 64N-90N       Arctic (the dramatic one)
#  - 24S-24N       Tropics
#  - 90S-64S       Antarctic (high south)
INTERESTING_BANDS = ("Glob", "NHem", "SHem", "64N-90N", "24S-24N", "90S-64S")


def parse(text: str) -> Optional[dict]:
    """Parse the wide-format CSV. Returns {bands: {name: {year: anomaly}}}."""
    lines = text.splitlines()
    header_idx = None
    header: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("Year"):
            header_idx = i
            header = [h.strip() for h in line.split(",")]
            break
    if header_idx is None:
        return None
    # Map column index → band name for the bands we care about
    cols: dict[int, str] = {}
    for ci, h in enumerate(header):
        if h in INTERESTING_BANDS:
            cols[ci] = h
    if not cols:
        return None
    bands: dict[str, dict[int, float]] = {b: {} for b in INTERESTING_BANDS if b in cols.values()}
    for line in lines[header_idx + 1:]:
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0]:
            continue
        try:
            year = int(parts[0])
        except ValueError:
            continue
        if not 1850 <= year <= 2100:
            continue
        for ci, band_name in cols.items():
            if ci >= len(parts):
                continue
            v = parts[ci]
            if not v or v == "***":
                continue
            try:
                bands[band_name][year] = round(float(v), 3)
            except ValueError:
                continue
    return {"bands": bands}


def fetch() -> Optional[dict]:
    cached = cache.get("gistemp_zonal")
    if cached is not None:
        return cached
    r = http.get(URL, timeout=30)
    if not r:
        return None
    parsed = parse(r.text)
    if not parsed or not parsed["bands"]:
        return None
    # Latest year present in the global band
    glob = parsed["bands"].get("Glob") or {}
    latest_year = max(glob.keys()) if glob else None
    out = {
        "source": SOURCE,
        "url": URL,
        "bands": parsed["bands"],
        "latest_year": latest_year,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache.set("gistemp_zonal", out)
    return out


def warming_ratios(zonal: Optional[dict], baseline_start: int = 1880,
                   baseline_end: int = 1910) -> Optional[dict]:
    """Compute warming since the early-record baseline for each band, plus
    the Arctic vs Global ratio.

    Returns {band: {anomaly_c, ratio_vs_global}} or None.
    """
    if not zonal or not zonal.get("bands"):
        return None
    bands = zonal["bands"]
    latest_year = zonal.get("latest_year")
    if not latest_year:
        return None

    def _band_warming(name: str) -> Optional[float]:
        data = bands.get(name) or {}
        if not data or latest_year not in data:
            return None
        base_years = [y for y in data if baseline_start <= y < baseline_end]
        # Real GISTEMP has every year; we only need a handful to compute a
        # stable mean. Falling back to whatever's in the baseline window if
        # we have fewer than 3 throws the result out as unreliable.
        if len(base_years) < 3:
            return None
        baseline_mean = sum(data[y] for y in base_years) / len(base_years)
        return data[latest_year] - baseline_mean

    global_w = _band_warming("Glob")
    if global_w is None or global_w == 0:
        return None
    out: dict[str, dict] = {}
    for name in INTERESTING_BANDS:
        w = _band_warming(name)
        if w is None:
            continue
        out[name] = {
            "anomaly_c": round(w, 2),
            "ratio_vs_global": round(w / global_w, 2),
        }
    return {
        "latest_year": latest_year,
        "baseline": f"{baseline_start}-{baseline_end - 1}",
        "bands": out,
    }
