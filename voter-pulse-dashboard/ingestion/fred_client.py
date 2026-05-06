"""FRED ingestion for everyday-life indicators.

Pulls a curated set of US monthly/weekly series from the public FRED CSV
endpoint (`fredgraph.csv`) — no API key required. The series here are the
ones that actually move how voters feel about their lives:

  Pocketbook (cost of living)
    CPIAUCSL          : Headline CPI (monthly, NSA)
    CPIUFDSL          : Food CPI (monthly, NSA)
    GASREGW           : Regular gas price, $/gal (weekly)
    MORTGAGE30US      : 30-year fixed mortgage rate (weekly)
    CSUSHPISA         : Case-Shiller US home price index (monthly)

  Wages and jobs
    UNRATE            : Unemployment rate (monthly)
    LES1252881600Q    : Real median weekly earnings, full-time (quarterly)
    PAYEMS            : Total non-farm payrolls (monthly)

  Sentiment
    UMCSENT           : University of Michigan consumer sentiment (monthly)

We keep a single in-process cache keyed by series id with a 12h TTL. CSVs
revise on a monthly cadence at most so this is plenty.
"""

from __future__ import annotations

import csv
import io
import logging
import time
import urllib.request
from dataclasses import dataclass
from threading import Lock

log = logging.getLogger(__name__)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
_UA = "voter-pulse-dashboard/0.1"

# (series_id, display label, group, units, "higher is better for voters?")
SERIES: list[tuple[str, str, str, str, bool]] = [
    ("CPIAUCSL",        "Headline CPI",          "pocketbook", "index",   False),
    ("CPIUFDSL",        "Food CPI",              "pocketbook", "index",   False),
    ("GASREGW",         "Gas price (regular)",   "pocketbook", "$/gal",   False),
    ("MORTGAGE30US",    "30-year mortgage rate", "pocketbook", "%",       False),
    ("CSUSHPISA",       "Home price index",      "pocketbook", "index",   False),
    ("UNRATE",          "Unemployment rate",     "jobs",       "%",       False),
    ("LES1252881600Q",  "Real median weekly earnings", "jobs", "$ (real)", True),
    ("PAYEMS",          "Non-farm payrolls",     "jobs",       "thous.",  True),
    ("DSPIC96",         "Real disposable income", "jobs",      "$bn (real)", True),
    ("UMCSENT",         "Consumer sentiment (UMich)", "sentiment", "index", True),
    ("MICH",            "Inflation expectations (1y)", "sentiment", "%",  False),
]


@dataclass
class IndicatorSeries:
    series_id: str
    label: str
    group: str
    units: str
    higher_is_better: bool
    points: list[tuple[str, float]]  # (ISO date, value)

    def latest(self) -> tuple[str, float] | None:
        return self.points[-1] if self.points else None

    def _delta_pct(self, years_back: int) -> float | None:
        """Percent change vs the observation from `years_back` years ago.

        We pick the closest observation whose ISO date is on-or-before
        (latest_date - years_back). This makes the metric robust to the
        differing cadences (weekly / monthly / quarterly) without faking
        precision we don't have.
        """
        if not self.points:
            return None
        latest_date, latest_val = self.points[-1]
        target_year = int(latest_date[:4]) - years_back
        target = f"{target_year:04d}{latest_date[4:]}"
        prior_val: float | None = None
        for d, v in self.points[:-1]:
            if d <= target:
                prior_val = v
            else:
                break
        if prior_val is None or prior_val == 0:
            return None
        return (latest_val - prior_val) / prior_val * 100.0

    def yoy_pct(self) -> float | None:
        return self._delta_pct(1)

    def four_year_pct(self) -> float | None:
        return self._delta_pct(4)

    def to_dict(self, max_points: int = 240) -> dict:
        pts = self.points[-max_points:] if max_points else self.points
        return {
            "series_id": self.series_id,
            "label": self.label,
            "group": self.group,
            "units": self.units,
            "higher_is_better": self.higher_is_better,
            "points": [{"date": d, "value": v} for d, v in pts],
            "latest": (
                {"date": self.points[-1][0], "value": self.points[-1][1]}
                if self.points else None
            ),
            "yoy_pct": self.yoy_pct(),
            "four_year_pct": self.four_year_pct(),
        }


def _fetch_csv(series_id: str, timeout: float = 15.0) -> str:
    url = FRED_CSV.format(series_id=series_id)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        return resp.read().decode("utf-8", errors="replace")


def _parse_csv(body: str) -> list[tuple[str, float]]:
    """FRED CSV format: header `DATE,<SERIES_ID>` then `YYYY-MM-DD,<value>`.

    Missing observations are encoded as `.` — drop those.
    """
    reader = csv.reader(io.StringIO(body))
    rows = list(reader)
    if len(rows) < 2:
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
    return _parse_csv(_fetch_csv(series_id))


def fetch_all() -> list[IndicatorSeries]:
    out: list[IndicatorSeries] = []
    for sid, label, group, units, higher_better in SERIES:
        try:
            points = fetch_series(sid)
            log.info("FRED %s: %d points", sid, len(points))
            out.append(IndicatorSeries(sid, label, group, units, higher_better, points))
        except Exception as exc:
            log.warning("FRED fetch failed for %s: %s", sid, exc)
            out.append(IndicatorSeries(sid, label, group, units, higher_better, []))
    return out


# ── Cache ────────────────────────────────────────────────────────────────────
_CACHE: dict = {"data": [], "fetched_at": 0.0}
_CACHE_TTL = 12 * 3600
_lock = Lock()


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = (now - _CACHE["fetched_at"]) < _CACHE_TTL and _CACHE["data"]
        if fresh and not force:
            return {
                "series": [s.to_dict() for s in _CACHE["data"]],
                "fetched_at": _CACHE["fetched_at"],
                "stale": False,
            }
    series = fetch_all()
    with _lock:
        _CACHE["data"] = series
        _CACHE["fetched_at"] = now
    return {
        "series": [s.to_dict() for s in series],
        "fetched_at": now,
        "stale": False,
    }


def get_series_by_id(series_id: str) -> IndicatorSeries | None:
    with _lock:
        for s in _CACHE["data"]:
            if s.series_id == series_id:
                return s
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = get_cached(force=True)
    for s in result["series"]:
        latest = s["latest"]
        yoy = s["yoy_pct"]
        yoy_s = f"{yoy:+.2f}%" if yoy is not None else "n/a"
        print(f"{s['series_id']:18s} {s['label']:32s} latest={latest}  yoy={yoy_s}")
