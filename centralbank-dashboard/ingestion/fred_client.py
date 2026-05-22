"""FRED policy-rate ingestion.

Pulls daily-frequency policy rate series from the public FRED CSV endpoint
(`fredgraph.csv`) — no API key required for the first cut. If we later need
revisions, vintages, or higher rate limits, switch to the keyed JSON API.

Series picked for v0:
  - DFF      : Federal Funds Effective Rate (US, daily)
  - ECBDFR   : ECB Deposit Facility Rate (EA, daily)
  - BOEBR    : Bank of England Bank Rate (UK, daily)

BoJ is intentionally omitted from v0 — its FRED proxies (discount rate,
overnight call rate) are messy and warrant a dedicated source. TODO: add BoJ
via a direct BoJ statistics scrape in step 2.
"""

from __future__ import annotations

import csv
import io
import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

log = logging.getLogger(__name__)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

# (series_id, display label, country code)
SERIES = [
    ("DFF",     "Fed Funds (US)",   "US"),
    ("ECBDFR",  "ECB Deposit (EA)", "EA"),
    ("BOEBR",   "BoE Bank Rate (UK)", "UK"),
]


@dataclass
class RateSeries:
    series_id: str
    label: str
    country: str
    points: list[tuple[str, float]]  # (ISO date, rate %)

    def to_dict(self) -> dict:
        return {
            "series_id": self.series_id,
            "label": self.label,
            "country": self.country,
            "points": [{"date": d, "rate": r} for d, r in self.points],
            "latest": self.points[-1] if self.points else None,
        }


def _fetch_csv(series_id: str, timeout: float = 15.0) -> str:
    url = FRED_CSV.format(series_id=series_id)
    req = urllib.request.Request(url, headers={"User-Agent": "centralbank-dashboard/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        return resp.read().decode("utf-8", errors="replace")


def _parse_csv(body: str, series_id: str) -> list[tuple[str, float]]:
    """FRED CSV format: header row `DATE,<SERIES_ID>` then `YYYY-MM-DD,<value>`.

    Missing observations are encoded as `.` — we drop those rather than
    forward-fill, since downstream code can step-interpolate for charting.
    """
    reader = csv.reader(io.StringIO(body))
    rows = list(reader)
    if not rows:
        return []
    header = rows[0]
    if len(header) < 2:
        log.warning("Unexpected FRED header for %s: %s", series_id, header)
        return []
    out: list[tuple[str, float]] = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        date_str, value_str = row[0], row[1]
        if value_str in (".", "", None):
            continue
        try:
            out.append((date_str, float(value_str)))
        except ValueError:
            continue
    return out


def fetch_series(series_id: str) -> list[tuple[str, float]]:
    body = _fetch_csv(series_id)
    return _parse_csv(body, series_id)


def fetch_all() -> list[RateSeries]:
    out: list[RateSeries] = []
    for series_id, label, country in SERIES:
        try:
            points = fetch_series(series_id)
            log.info("FRED %s: %d points", series_id, len(points))
            out.append(RateSeries(series_id, label, country, points))
        except Exception as exc:  # network errors, parse errors — keep going
            log.warning("FRED fetch failed for %s: %s", series_id, exc)
            out.append(RateSeries(series_id, label, country, []))
    return out


# ── Cache ────────────────────────────────────────────────────────────────────
_CACHE: dict = {"data": [], "fetched_at": 0.0}
_CACHE_TTL = 6 * 3600  # 6 hours — policy rates change at most daily
_lock = Lock()


def get_cached_rates(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = (now - _CACHE["fetched_at"]) < _CACHE_TTL and _CACHE["data"]
        if fresh and not force:
            return {
                "series": [s.to_dict() for s in _CACHE["data"]],
                "fetched_at": _CACHE["fetched_at"],
                "stale": False,
            }
    # Fetch outside the lock
    series = fetch_all()
    with _lock:
        _CACHE["data"] = series
        _CACHE["fetched_at"] = now
    return {
        "series": [s.to_dict() for s in series],
        "fetched_at": now,
        "stale": False,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = get_cached_rates(force=True)
    for s in result["series"]:
        latest = s["latest"]
        print(f"{s['series_id']:8s} {s['label']:25s} latest={latest}  n={len(s['points'])}")
