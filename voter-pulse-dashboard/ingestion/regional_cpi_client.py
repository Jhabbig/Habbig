"""Regional CPI ingestion.

The BLS publishes a CPI series per Census region, hosted on FRED as
seasonally-unadjusted "All items, by region" indices. We pull the four
mainland regions (Northeast / Midwest / South / West); regional CPI
combined with regional unemployment (already collected via state_client)
gives us a clean four-way "for-you" cut of the pocketbook and jobs
sub-scores.

  CUUR0100SA0  - Northeast, all items
  CUUR0200SA0  - Midwest,   all items
  CUUR0300SA0  - South,     all items
  CUUR0400SA0  - West,      all items

Same shared FRED CSV endpoint as the rest of the dashboard - no API
key. Cached 12h alongside the other monthly series.
"""

from __future__ import annotations

import csv
import io
import logging
import time
import urllib.request
from threading import Lock

log = logging.getLogger(__name__)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
_UA = "voter-pulse-dashboard/0.4"

# (region label matching states_client.STATES[].region, FRED series id)
REGIONS: list[tuple[str, str]] = [
    ("Northeast", "CUUR0100SA0"),
    ("Midwest",   "CUUR0200SA0"),
    ("South",     "CUUR0300SA0"),
    ("West",      "CUUR0400SA0"),
]


def _fetch_csv(series_id: str, timeout: float = 15.0) -> str:
    url = FRED_CSV.format(series_id=series_id)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def _parse_csv(body: str) -> list[tuple[str, float]]:
    rows = list(csv.reader(io.StringIO(body)))
    if len(rows) < 2:
        return []
    out: list[tuple[str, float]] = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        date_str, val_str = row[0], row[1]
        if val_str in (".", "", None):
            continue
        try:
            out.append((date_str, float(val_str)))
        except ValueError:
            continue
    return out


def _yoy_pct(points: list[tuple[str, float]]) -> float | None:
    """Year-over-year percent change of the latest observation."""
    if len(points) < 13:
        return None
    latest_date, latest_val = points[-1]
    target_year = int(latest_date[:4]) - 1
    target_iso = f"{target_year:04d}{latest_date[4:]}"
    prior: float | None = None
    for d, v in points[:-1]:
        if d <= target_iso:
            prior = v
        else:
            break
    if prior is None or prior == 0:
        return None
    return (latest_val - prior) / prior * 100.0


def fetch_one(label: str, series_id: str) -> dict:
    try:
        points = _parse_csv(_fetch_csv(series_id))
    except Exception as exc:
        log.warning("regional CPI fetch failed %s (%s): %s", label, series_id, exc)
        points = []
    return {
        "region": label,
        "series_id": series_id,
        "latest": (
            {"date": points[-1][0], "value": points[-1][1]} if points else None
        ),
        "yoy_pct": _yoy_pct(points),
    }


def fetch_all() -> list[dict]:
    return [fetch_one(label, sid) for label, sid in REGIONS]


# ── Cache ────────────────────────────────────────────────────────────────────
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_CACHE_TTL = 12 * 3600
_lock = Lock()


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = (now - _CACHE["fetched_at"]) < _CACHE_TTL and _CACHE["data"] is not None
        if fresh and not force and _CACHE["fetched_at"]:
            return {"regions": _CACHE["data"], "fetched_at": _CACHE["fetched_at"]}
    rows = fetch_all()
    with _lock:
        _CACHE["data"] = rows
        _CACHE["fetched_at"] = now
    return {"regions": rows, "fetched_at": now}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = get_cached(force=True)
    for r in out["regions"]:
        latest = r["latest"]
        yoy = r["yoy_pct"]
        yoy_s = f"{yoy:+.2f}%" if yoy is not None else "n/a"
        print(f"{r['region']:10s} {r['series_id']:14s} latest={latest} yoy={yoy_s}")
