"""Presidential-administration era slicing.

We want to let readers see "what was CPI YoY / UNRATE / UMCSENT / the mood
index like under each president?" The actual mechanic is a left-inclusive,
right-exclusive slice of any monthly series, averaged.

Inauguration dates are hard-coded — they're not data, they're history. We
include only post-1947 eras because most of our FRED series don't go back
further (UMCSENT starts 1952, others 1948).
"""

from __future__ import annotations

from dataclasses import dataclass

# (label, party, start ISO date, end ISO date — exclusive; None = today)
ERAS: list[tuple[str, str, str, str | None]] = [
    ("Truman",     "D", "1947-01-20", "1953-01-20"),
    ("Eisenhower", "R", "1953-01-20", "1961-01-20"),
    ("Kennedy/LBJ", "D", "1961-01-20", "1969-01-20"),
    ("Nixon/Ford", "R", "1969-01-20", "1977-01-20"),
    ("Carter",     "D", "1977-01-20", "1981-01-20"),
    ("Reagan",     "R", "1981-01-20", "1989-01-20"),
    ("Bush Sr.",   "R", "1989-01-20", "1993-01-20"),
    ("Clinton",    "D", "1993-01-20", "2001-01-20"),
    ("Bush Jr.",   "R", "2001-01-20", "2009-01-20"),
    ("Obama",      "D", "2009-01-20", "2017-01-20"),
    ("Trump I",    "R", "2017-01-20", "2021-01-20"),
    ("Biden",      "D", "2021-01-20", "2025-01-20"),
    ("Trump II",   "R", "2025-01-20", None),
]


@dataclass
class EraStat:
    label: str
    party: str
    start: str
    end: str | None
    n_points: int
    mean: float | None
    min: float | None
    max: float | None


def slice_series(points: list[dict], start: str, end: str | None) -> list[float]:
    """Return all values whose ISO date falls in [start, end)."""
    out: list[float] = []
    for p in points:
        d = p.get("date") or ""
        if d < start:
            continue
        if end is not None and d >= end:
            continue
        v = p.get("value")
        if v is None:
            continue
        out.append(float(v))
    return out


def stats_for_series(series: dict) -> list[dict]:
    """Per-era summary stats for one indicator series payload."""
    points = series.get("points") or []
    rows: list[dict] = []
    for label, party, start, end in ERAS:
        vals = slice_series(points, start, end)
        rows.append({
            "label": label,
            "party": party,
            "start": start,
            "end": end,
            "n_points": len(vals),
            "mean": sum(vals) / len(vals) if vals else None,
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
        })
    return rows


def compose(series_payload: list[dict], series_ids: list[str]) -> dict:
    """For each named series id, return a per-era summary."""
    by_id = {s["series_id"]: s for s in series_payload}
    out: dict[str, list[dict]] = {}
    for sid in series_ids:
        s = by_id.get(sid)
        if not s:
            continue
        out[sid] = stats_for_series(s)
    return {
        "eras": [
            {"label": l, "party": p, "start": s, "end": e}
            for (l, p, s, e) in ERAS
        ],
        "series": out,
    }
