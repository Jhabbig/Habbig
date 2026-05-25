"""US economic-release tracker — the prints that move FOMC expectations.

Why this matters for the dashboard:
  CPI / Core CPI / PCE / Core PCE / NFP releases shift Fed-rate expectations
  *more* than statements do. A +0.3 pp upside surprise on Core CPI can re-
  price the Polymarket FOMC market by 5-10 pp in minutes. Surfacing the
  release calendar + latest YoY trajectories lets the user see where
  expectations are anchored and when the next data point lands.

What we ship in v0.7:
  * Latest reading + YoY % change + 12-month sparkline data per series
  * Approximate next-release date computed from release-day conventions
  * Days-until countdown

What we explicitly *don't* ship (yet):
  * **Consensus vs actual surprise tracking**. Bloomberg / Refinitiv consensus
    feeds are paid; free scrapers (Investing.com, Trading Economics) are
    fragile across redesigns. Phase 2 of this module — until then we show
    actuals only and let the user compare against their own consensus from
    elsewhere.

Sources (all free, no key):
  * FRED CSV — same endpoint :mod:`fred_client` already uses for policy rates
  * Release-day conventions — encoded as Python rules, not scraped

Cache: 6 hours. These series update at most monthly, so tighter just
hammers FRED.
"""

from __future__ import annotations

import calendar as _cal
import csv
import io
import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from threading import Lock

log = logging.getLogger(__name__)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
_UA = "centralbank-dashboard/0.7"

_CACHE: dict = {"data": None, "fetched_at": 0.0}
_CACHE_TTL = 6 * 3600  # 6 h
_lock = Lock()


@dataclass
class Series:
    fred_id: str
    name: str
    short_name: str          # for UI badges
    release_kind: str        # "CPI" | "PCE" | "NFP"
    is_index: bool           # True ⇒ report YoY % change; False ⇒ raw level + MoM delta (NFP)
    units: str               # "%" | "k jobs" — purely cosmetic


SERIES: list[Series] = [
    Series("CPIAUCSL",  "Headline CPI",         "CPI",      "CPI", True,  "%"),
    Series("CPILFESL",  "Core CPI",             "Core CPI", "CPI", True,  "%"),
    Series("PCEPI",     "Headline PCE",         "PCE",      "PCE", True,  "%"),
    Series("PCEPILFE",  "Core PCE",             "Core PCE", "PCE", True,  "%"),
    Series("PAYEMS",    "Non-Farm Payrolls",    "NFP",      "NFP", False, "k jobs"),
]


# --- FRED CSV helpers (same shape as fred_client) ---------------------------

def _fetch_csv(series_id: str, timeout: float = 15.0) -> str | None:
    url = FRED_CSV.format(series_id=series_id)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("FRED %s fetch failed: %s", series_id, exc)
        return None


def _parse_csv(body: str) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for row in csv.reader(io.StringIO(body)):
        if len(row) < 2 or row[0] == "DATE":
            continue
        try:
            out.append((row[0], float(row[1])))
        except ValueError:
            continue
    return out


# --- Release-date conventions ----------------------------------------------

def _first_friday(year: int, month: int) -> date:
    """First Friday of (year, month). NFP convention."""
    d = date(year, month, 1)
    # weekday(): Mon=0 … Fri=4
    delta = (4 - d.weekday()) % 7
    return d + timedelta(days=delta)


def _next_business_day(d: date) -> date:
    """If date falls on weekend, push to Monday. Conservative for ET market hours."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _next_release_date(kind: str, today: date | None = None) -> date | None:
    """Approximate next release date for a given series kind.

    Conventions used (verified against BLS / BEA 2023-2026 patterns):
      * CPI: ~13th business day of the month, normally the 13th–15th calendar
        day. We use the 14th and bump off weekends.
      * PCE: 4th business day after the 25th of the month — typically falls
        on the 28th–30th. We use the 29th and bump off weekends.
      * NFP (Employment Situation): first Friday of the month.
    """
    today = today or datetime.now(timezone.utc).date()
    # Find the next month-of-release that has a release date strictly after today.
    for offset in range(0, 4):
        y, m = today.year, today.month + offset
        while m > 12:
            m -= 12
            y += 1
        try:
            if kind == "CPI":
                d = _next_business_day(date(y, m, 14))
            elif kind == "PCE":
                d = _next_business_day(date(y, m, 29))
            elif kind == "NFP":
                d = _first_friday(y, m)
            else:
                return None
        except ValueError:
            continue
        if d >= today:
            return d
    return None


# --- Compute summary ---------------------------------------------------------

def _compute_yoy(points: list[tuple[str, float]]) -> tuple[dict | None, dict | None, list[float]]:
    """Return (latest_summary, prev_summary, last_24m_yoy_series)."""
    if len(points) < 13:
        return None, None, []

    # latest: most recent observation
    last_date, last_val = points[-1]

    # 12-month-ago observation: closest by date
    last_d = date.fromisoformat(last_date)
    target_d = date(last_d.year - 1, last_d.month, last_d.day)
    yoy_idx = None
    for i, (ds, _) in enumerate(points):
        if ds <= target_d.isoformat():
            yoy_idx = i
    if yoy_idx is None:
        return None, None, []
    yoy_val = points[yoy_idx][1]
    yoy_pct = (last_val / yoy_val - 1) * 100 if yoy_val else 0.0

    prev_date, prev_val = points[-2]
    mom_pct = (last_val / prev_val - 1) * 100 if prev_val else 0.0

    # 24-month rolling YoY for sparkline
    sparkline: list[float] = []
    for i in range(max(0, len(points) - 24), len(points)):
        ds, v = points[i]
        d = date.fromisoformat(ds)
        anchor = date(d.year - 1, d.month, d.day).isoformat()
        # Walk backwards from i to find the closest earlier-or-equal anchor
        anchor_v = None
        for j in range(i, -1, -1):
            if points[j][0] <= anchor:
                anchor_v = points[j][1]
                break
        if anchor_v:
            sparkline.append(round((v / anchor_v - 1) * 100, 3))

    return (
        {"date": last_date, "value": round(last_val, 4),
         "yoy_pct": round(yoy_pct, 3), "mom_pct": round(mom_pct, 3)},
        {"date": prev_date, "value": round(prev_val, 4)},
        sparkline,
    )


def _compute_nfp(points: list[tuple[str, float]]) -> tuple[dict | None, dict | None, list[float]]:
    """For NFP we report monthly *change* in jobs, not YoY %. PAYEMS is in
    thousands, so the change is "thousand of jobs added"."""
    if len(points) < 2:
        return None, None, []
    last_date, last_val = points[-1]
    prev_date, prev_val = points[-2]
    mom_change = last_val - prev_val
    sparkline: list[float] = []
    for i in range(max(1, len(points) - 24), len(points)):
        sparkline.append(round(points[i][1] - points[i - 1][1], 1))
    return (
        {"date": last_date, "value": round(last_val, 1),
         "mom_change_k": round(mom_change, 1)},
        {"date": prev_date, "value": round(prev_val, 1)},
        sparkline,
    )


def fetch_all() -> list[dict]:
    today = datetime.now(timezone.utc).date()
    rows: list[dict] = []
    for s in SERIES:
        body = _fetch_csv(s.fred_id)
        points = _parse_csv(body) if body else []
        if s.is_index:
            latest, prev, sparkline = _compute_yoy(points)
        else:
            latest, prev, sparkline = _compute_nfp(points)

        next_d = _next_release_date(s.release_kind, today)
        days_until = (next_d - today).days if next_d else None

        rows.append({
            "fred_id": s.fred_id,
            "name": s.name,
            "short_name": s.short_name,
            "release_kind": s.release_kind,
            "units": s.units,
            "is_index": s.is_index,
            "latest": latest,
            "prev": prev,
            "sparkline": sparkline,                    # 24-pt series for chart
            "next_release_date": next_d.isoformat() if next_d else None,
            "days_until": days_until,
        })
    return rows


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = _CACHE["data"] is not None and (now - _CACHE["fetched_at"]) < _CACHE_TTL
        if fresh and not force:
            return _CACHE["data"]
    rows = fetch_all()
    today = datetime.now(timezone.utc).date()
    data = {
        "as_of": today.isoformat(),
        "series": rows,
        "release_calendar_method": (
            "CPI ≈ 14th of month (BLS); PCE ≈ 29th of month (BEA); "
            "NFP = 1st Friday of next month (BLS); weekend-bumped to Mon"
        ),
    }
    with _lock:
        _CACHE["data"] = data
        _CACHE["fetched_at"] = now
    return data


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(get_cached(force=True), indent=2)[:3000])
