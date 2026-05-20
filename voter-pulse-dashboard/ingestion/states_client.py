"""State-level unemployment ingestion from FRED.

Every US state (plus DC) has a seasonally-adjusted unemployment rate series on
FRED with the pattern `{ST}UR` — `CAUR` for California, `TXUR` for Texas, and
so on. Like the national `UNRATE` these update monthly, around the 3rd Friday.

We fetch all 51 series in parallel-ish (sequentially with a small delay isn't
worth the complexity here), parse the FRED CSV format, and cache the bundle for
12 hours. If any individual state fetch fails the others still render — the
map is allowed to have grey tiles.
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
_UA = "voter-pulse-dashboard/0.3"


# (postal code, full name, FRED series id, region)
STATES: list[tuple[str, str, str, str]] = [
    ("AL", "Alabama",       "ALUR", "South"),
    ("AK", "Alaska",        "AKUR", "West"),
    ("AZ", "Arizona",       "AZUR", "West"),
    ("AR", "Arkansas",      "ARUR", "South"),
    ("CA", "California",    "CAUR", "West"),
    ("CO", "Colorado",      "COUR", "West"),
    ("CT", "Connecticut",   "CTUR", "Northeast"),
    ("DE", "Delaware",      "DEUR", "South"),
    ("DC", "Dist. of Columbia", "DCUR", "South"),
    ("FL", "Florida",       "FLUR", "South"),
    ("GA", "Georgia",       "GAUR", "South"),
    ("HI", "Hawaii",        "HIUR", "West"),
    ("ID", "Idaho",         "IDUR", "West"),
    ("IL", "Illinois",      "ILUR", "Midwest"),
    ("IN", "Indiana",       "INUR", "Midwest"),
    ("IA", "Iowa",          "IAUR", "Midwest"),
    ("KS", "Kansas",        "KSUR", "Midwest"),
    ("KY", "Kentucky",      "KYUR", "South"),
    ("LA", "Louisiana",     "LAUR", "South"),
    ("ME", "Maine",         "MEUR", "Northeast"),
    ("MD", "Maryland",      "MDUR", "South"),
    ("MA", "Massachusetts", "MAUR", "Northeast"),
    ("MI", "Michigan",      "MIUR", "Midwest"),
    ("MN", "Minnesota",     "MNUR", "Midwest"),
    ("MS", "Mississippi",   "MSUR", "South"),
    ("MO", "Missouri",      "MOUR", "Midwest"),
    ("MT", "Montana",       "MTUR", "West"),
    ("NE", "Nebraska",      "NEUR", "Midwest"),
    ("NV", "Nevada",        "NVUR", "West"),
    ("NH", "New Hampshire", "NHUR", "Northeast"),
    ("NJ", "New Jersey",    "NJUR", "Northeast"),
    ("NM", "New Mexico",    "NMUR", "West"),
    ("NY", "New York",      "NYUR", "Northeast"),
    ("NC", "North Carolina","NCUR", "South"),
    ("ND", "North Dakota",  "NDUR", "Midwest"),
    ("OH", "Ohio",          "OHUR", "Midwest"),
    ("OK", "Oklahoma",      "OKUR", "South"),
    ("OR", "Oregon",        "ORUR", "West"),
    ("PA", "Pennsylvania",  "PAUR", "Northeast"),
    ("RI", "Rhode Island",  "RIUR", "Northeast"),
    ("SC", "South Carolina","SCUR", "South"),
    ("SD", "South Dakota",  "SDUR", "Midwest"),
    ("TN", "Tennessee",     "TNUR", "South"),
    ("TX", "Texas",         "TXUR", "South"),
    ("UT", "Utah",          "UTUR", "West"),
    ("VT", "Vermont",       "VTUR", "Northeast"),
    ("VA", "Virginia",      "VAUR", "South"),
    ("WA", "Washington",    "WAUR", "West"),
    ("WV", "West Virginia", "WVUR", "South"),
    ("WI", "Wisconsin",     "WIUR", "Midwest"),
    ("WY", "Wyoming",       "WYUR", "West"),
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
        date_str, value_str = row[0], row[1]
        if value_str in (".", "", None):
            continue
        try:
            out.append((date_str, float(value_str)))
        except ValueError:
            continue
    return out


def _delta_pct(points: list[tuple[str, float]], years_back: int) -> float | None:
    if not points:
        return None
    latest_date, latest_val = points[-1]
    target_year = int(latest_date[:4]) - years_back
    target_iso = f"{target_year:04d}{latest_date[4:]}"
    prior_val: float | None = None
    for d, v in points[:-1]:
        if d <= target_iso:
            prior_val = v
        else:
            break
    if prior_val is None or prior_val == 0:
        return None
    return (latest_val - prior_val) / prior_val * 100.0


def _delta_pp(points: list[tuple[str, float]], years_back: int) -> float | None:
    """Percentage-point change (not percent). Unemployment is already in %,
    so a percentage-point change is the intuitive comparison."""
    if not points:
        return None
    latest_date, latest_val = points[-1]
    target_year = int(latest_date[:4]) - years_back
    target_iso = f"{target_year:04d}{latest_date[4:]}"
    prior_val: float | None = None
    for d, v in points[:-1]:
        if d <= target_iso:
            prior_val = v
        else:
            break
    if prior_val is None:
        return None
    return latest_val - prior_val


def fetch_state(postal: str, name: str, series_id: str, region: str) -> dict:
    try:
        points = _parse_csv(_fetch_csv(series_id))
    except Exception as exc:
        log.warning("state fetch failed for %s (%s): %s", postal, series_id, exc)
        points = []
    return {
        "postal": postal,
        "name": name,
        "series_id": series_id,
        "region": region,
        "latest": (
            {"date": points[-1][0], "value": points[-1][1]} if points else None
        ),
        "delta_1y_pp": _delta_pp(points, 1),
        "delta_4y_pp": _delta_pp(points, 4),
    }


def fetch_all_states() -> list[dict]:
    out: list[dict] = []
    for postal, name, sid, region in STATES:
        out.append(fetch_state(postal, name, sid, region))
    return out


# ── Cache ────────────────────────────────────────────────────────────────────
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_CACHE_TTL = 12 * 3600
_lock = Lock()


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = (now - _CACHE["fetched_at"]) < _CACHE_TTL and _CACHE["data"] is not None
        if fresh and not force and _CACHE["fetched_at"]:
            return {**_CACHE["data"], "fetched_at": _CACHE["fetched_at"]}
    states = fetch_all_states()
    # National benchmark (median + mean) — used by the UI to color tiles
    vals = [s["latest"]["value"] for s in states if s.get("latest")]
    benchmark = {
        "n": len(vals),
        "mean":   (sum(vals) / len(vals)) if vals else None,
        "median": sorted(vals)[len(vals) // 2] if vals else None,
        "min":    min(vals) if vals else None,
        "max":    max(vals) if vals else None,
    }
    data = {"states": states, "benchmark": benchmark}
    with _lock:
        _CACHE["data"] = data
        _CACHE["fetched_at"] = now
    return {**data, "fetched_at": now}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = get_cached(force=True)
    print(f"{len(out['states'])} states fetched; benchmark: {out['benchmark']}")
    for s in sorted([x for x in out["states"] if x["latest"]],
                    key=lambda r: r["latest"]["value"])[:5]:
        print(f"  {s['postal']} {s['name']:18s} {s['latest']['value']:.1f}%  "
              f"(Δ1y {s['delta_1y_pp']:+.1f}pp, Δ4y {s['delta_4y_pp']:+.1f}pp)")
