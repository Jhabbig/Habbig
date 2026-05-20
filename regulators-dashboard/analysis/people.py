"""Personnel-watch loader — v0.6.

Loads the hand-curated roster from `data/personnel.py`, computes
`days_until` for each term-end, and orders rows by upcoming-ness so the
dashboard can surface "Powell's term ends in 3 days" at the top.

Also constructs a synthetic item per person so the v0.5 market matcher
can attach Polymarket / Kalshi markets — "Will Powell be Fed Chair on
Dec 31, 2026?" surfaces against the Powell row without any new matcher
code. ANCHOR_TOKENS already includes the surnames we depend on.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from data.personnel import PEOPLE


def _days_until(iso_date: str, ref: date) -> int | None:
    if not iso_date:
        return None
    try:
        target = date.fromisoformat(iso_date)
    except ValueError:
        return None
    return (target - ref).days


def _sort_key(p: dict) -> tuple[int, int]:
    """Imminent future first, then later future, then past, then unknown."""
    d = p["days_until"]
    if d is None:
        return (3, 0)
    if d < 0:
        return (2, -d)        # past entries, most-recent first
    if d <= 365:
        return (0, d)         # within a year: imminent bucket
    return (1, d)             # beyond a year: later bucket


def roster() -> list[dict]:
    today = datetime.now(timezone.utc).date()
    out: list[dict] = []
    for p in PEOPLE:
        days = _days_until(p["term_end"], ref=today)
        out.append({
            **p,
            "days_until": days,
            "is_past":    days is not None and days < 0,
        })
    out.sort(key=_sort_key)
    return out


def synthetic_item_for(person: dict) -> dict:
    """Render a person as a fake feed-item so the v0.5 matcher can run."""
    title = f"{person['name']} — {person['role']} {person['regulator']}"
    summary = person.get("notes", "")
    return {"title": title, "summary": summary}


if __name__ == "__main__":
    import json
    print(json.dumps(roster(), indent=2)[:2000])
