"""Central bank decision calendar.

v0.1: hand-curated meeting dates for the major CBs we cover. Each CB
publishes its schedule a year in advance, so a yearly manual refresh is fine
for a first cut. v0.2 should scrape each CB's calendar page so dates auto-
update (especially when a meeting gets moved, which does happen).

For multi-day meetings (FOMC: Tue–Wed, BoE MPC: Wed–Thu), `decision_date` is
the day the rate decision is announced — that's the market-moving instant.

⚠ VERIFY DATES AGAINST OFFICIAL SOURCES BEFORE TRADING ON THEM. ⚠
- FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
- ECB:  https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html
- BoE:  https://www.bankofengland.co.uk/monetary-policy/monetary-policy-summary-and-minutes
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

CB = Literal["US", "EA", "UK"]

# 2026 meeting dates (YYYY-MM-DD, decision announcement date).
# Source-of-record per CB above. Update annually.
MEETINGS_2026: list[tuple[CB, str, str]] = [
    # (CB, decision_date, label)
    ("US", "2026-01-28", "FOMC"),
    ("US", "2026-03-18", "FOMC"),
    ("US", "2026-04-29", "FOMC"),
    ("US", "2026-06-17", "FOMC"),
    ("US", "2026-07-29", "FOMC"),
    ("US", "2026-09-16", "FOMC"),
    ("US", "2026-10-28", "FOMC"),
    ("US", "2026-12-09", "FOMC"),

    ("EA", "2026-01-22", "ECB Governing Council"),
    ("EA", "2026-03-05", "ECB Governing Council"),
    ("EA", "2026-04-16", "ECB Governing Council"),
    ("EA", "2026-06-04", "ECB Governing Council"),
    ("EA", "2026-07-23", "ECB Governing Council"),
    ("EA", "2026-09-10", "ECB Governing Council"),
    ("EA", "2026-10-22", "ECB Governing Council"),
    ("EA", "2026-12-17", "ECB Governing Council"),

    ("UK", "2026-02-05", "BoE MPC"),
    ("UK", "2026-03-19", "BoE MPC"),
    ("UK", "2026-05-07", "BoE MPC"),
    ("UK", "2026-06-18", "BoE MPC"),
    ("UK", "2026-08-06", "BoE MPC"),
    ("UK", "2026-09-17", "BoE MPC"),
    ("UK", "2026-11-05", "BoE MPC"),
    ("UK", "2026-12-17", "BoE MPC"),
]

CB_LABELS: dict[CB, str] = {
    "US": "Federal Reserve",
    "EA": "European Central Bank",
    "UK": "Bank of England",
}


def upcoming(today: date | None = None, horizon_days: int = 90) -> list[dict]:
    """Return meetings within `horizon_days` of today, sorted ascending.

    Includes today itself (decision-day FOMC at 2pm ET still counts as upcoming
    for most of the trading day).
    """
    today = today or datetime.now(timezone.utc).date()
    out: list[dict] = []
    for cb, ds, label in MEETINGS_2026:
        d = date.fromisoformat(ds)
        delta = (d - today).days
        if 0 <= delta <= horizon_days:
            out.append({
                "cb": cb,
                "cb_name": CB_LABELS[cb],
                "label": label,
                "decision_date": ds,
                "days_until": delta,
            })
    out.sort(key=lambda m: m["decision_date"])
    return out


def get_calendar(horizon_days: int = 90) -> dict:
    today = datetime.now(timezone.utc).date()
    return {
        "as_of": today.isoformat(),
        "horizon_days": horizon_days,
        "meetings": upcoming(today, horizon_days),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(get_calendar(), indent=2))
