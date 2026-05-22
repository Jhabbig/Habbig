"""Recent-release feed.

Walks every FRED series the dashboard tracks and surfaces the most
recent observation per series, sorted by recency. For each row we
compute the most useful deltas (period-over-period and year-over-year)
and label how long ago the release was so the UI can render "3 days
ago", "a fortnight ago", etc.

This panel exists to give returning readers a reason to come back daily:
"Has anything happened since I last looked?" The feed is constructed
purely from the data already cached by `fred_client` — no extra fetches.
"""

from __future__ import annotations

from datetime import datetime, timezone


# How far back an observation can be and still count as "recent" enough to
# surface in the feed. 90 days catches every monthly cadence with comfortable
# slack — CPI observations are typically published a month after the period
# they refer to, so we want the window to cover the lag plus a buffer.
RECENT_DAYS = 90


def _parse_iso(date_str: str) -> datetime | None:
    if not date_str or len(date_str) < 10:
        return None
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _human_ago(then: datetime, now: datetime) -> str:
    delta = now - then
    days = delta.days
    if days < 0:
        return "in the future"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 14:
        return "last week"
    if days < 32:
        return f"{days // 7} weeks ago"
    if days < 75:
        return f"about {days // 30} month{'s' if days // 30 > 1 else ''} ago"
    return f"{days // 30} months ago"


def _series_release_row(series: dict, now: datetime) -> dict | None:
    points = series.get("points") or []
    if not points:
        return None
    latest = points[-1]
    obs_date = _parse_iso(latest.get("date") or "")
    if obs_date is None:
        return None
    prior = points[-2] if len(points) >= 2 else None
    prior_value = prior.get("value") if prior else None
    pop_change = None  # period-over-period change (absolute)
    pop_change_pct = None
    if prior_value not in (None, 0):
        pop_change = latest["value"] - prior_value
        pop_change_pct = pop_change / prior_value * 100.0
    return {
        "series_id": series["series_id"],
        "label": series["label"],
        "units": series.get("units"),
        "group": series.get("group"),
        "higher_is_better": series.get("higher_is_better"),
        "observation_date": latest["date"],
        "value": latest["value"],
        "prior_value": prior_value,
        "pop_change": pop_change,
        "pop_change_pct": pop_change_pct,
        "yoy_pct": series.get("yoy_pct"),
        "days_ago": (now.date() - obs_date.date()).days,
        "ago_text": _human_ago(obs_date, now),
    }


def compose(life_payload: dict) -> dict:
    """Build the release feed from the existing FRED payload.

    Returns:
      releases  : list of recent-release rows, newest first
      stale     : list of series with no observation in the recent window
      generated_at_iso : ISO timestamp the feed was computed
    """
    now = datetime.now(timezone.utc)
    series_list = life_payload.get("series") or []

    releases: list[dict] = []
    stale: list[dict] = []
    for s in series_list:
        row = _series_release_row(s, now)
        if not row:
            continue
        if row["days_ago"] <= RECENT_DAYS:
            releases.append(row)
        else:
            stale.append({
                "series_id": row["series_id"],
                "label": row["label"],
                "observation_date": row["observation_date"],
                "days_ago": row["days_ago"],
            })

    # Newest first; tie-break by absolute headline movement so a fresh CPI
    # release with a real move beats an unchanged-from-prior reading.
    releases.sort(
        key=lambda r: (-r["days_ago"], abs(r["pop_change"] or 0.0)),
        reverse=True,
    )
    return {
        "releases": releases,
        "stale_count": len(stale),
        "stale": stale[:5],
        "recent_window_days": RECENT_DAYS,
        "generated_at_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
