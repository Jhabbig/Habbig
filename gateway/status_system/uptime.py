"""Uptime aggregation over the snapshot table.

The monitoring cron writes one row per component per minute. These helpers
read those rows and produce:

    * a per-day uptime_pct series (for the 90-day bar on /status)
    * a rolled-up overall uptime_pct + downtime_minutes + incident count
    * a system-wide "all operational / degraded / outage" banner state

Aggregation happens in Python — SQL can handle the date bucketing, but
keeping the logic in one place (and in Python) makes the tests easy.
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Optional

from status_system import COMPONENT_KEYS, STATUSES
from status_system import db as status_db


# Status → "was up this minute" weight. `degraded` counts as 50% uptime
# because the user-visible experience is partially working.
_UPTIME_WEIGHT = {
    "operational": 1.0,
    "degraded": 0.5,
    "outage": 0.0,
}


def _day_bounds_utc(date: _dt.date) -> tuple[int, int]:
    """Return (start_epoch, end_epoch_exclusive) for a given UTC date."""
    start = _dt.datetime(date.year, date.month, date.day, tzinfo=_dt.timezone.utc)
    end = start + _dt.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _today_utc() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def _snapshots_by_day(
    component: str, n_days: int, now: Optional[int] = None
) -> dict[_dt.date, list[dict]]:
    """Fetch all snapshots for `component` in the last `n_days` days and
    bucket them by UTC date. Returns a dict keyed by date with empty
    lists for days that have no snapshots.
    """
    today = _today_utc()
    earliest = today - _dt.timedelta(days=n_days - 1)
    since_ts, _ = _day_bounds_utc(earliest)
    until_ts = int(now if now is not None else time.time())

    snapshots = status_db.list_snapshots_since(component, since_ts, until_ts)

    buckets: dict[_dt.date, list[dict]] = {}
    for d in range(n_days):
        buckets[earliest + _dt.timedelta(days=d)] = []

    for snap in snapshots:
        ts = snap["timestamp"]
        day = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).date()
        if day in buckets:
            buckets[day].append(snap)
    return buckets


def _day_uptime_pct(snaps: list[dict]) -> Optional[float]:
    """Return 0–100 uptime % for a bucket of snapshots. None if empty."""
    if not snaps:
        return None
    total_weight = sum(_UPTIME_WEIGHT.get(s["status"], 0.0) for s in snaps)
    return round(100.0 * total_weight / len(snaps), 4)


def _day_status(snaps: list[dict]) -> str:
    """Collapse a day's snapshots to a single status label."""
    if not snaps:
        return "unknown"
    # Worst-case wins — one outage snap makes the whole day "outage"
    # in the tooltip, even if the rest were fine. Mirrors the user's
    # perception: 10 minutes of downtime is remembered as "an outage",
    # not "99.3% uptime".
    seen = {s["status"] for s in snaps}
    if "outage" in seen:
        return "outage"
    if "degraded" in seen:
        return "degraded"
    return "operational"


def compute_uptime_last_n_days(
    component: str, n: int = 90, *, now: Optional[int] = None
) -> dict:
    """Compute the uptime report for one component.

    Returns:
        {
          "component": "app",
          "uptime_pct": 99.94,
          "total_minutes": int,
          "downtime_minutes": int,
          "incidents": int,
          "daily_data": [
              {"date": "2026-04-20", "uptime_pct": 100.0, "status": "operational"},
              ...
          ]
        }

    Days with no snapshots at all are still returned but with
    `uptime_pct=None` and `status="unknown"`.
    """
    if component not in COMPONENT_KEYS:
        raise ValueError(f"unknown component: {component!r}")

    buckets = _snapshots_by_day(component, n, now=now)

    daily_data: list[dict] = []
    running_total = 0.0
    running_weight = 0
    for day in sorted(buckets.keys()):
        snaps = buckets[day]
        pct = _day_uptime_pct(snaps)
        stat = _day_status(snaps)
        daily_data.append({
            "date": day.isoformat(),
            "uptime_pct": pct,
            "status": stat,
            "snapshot_count": len(snaps),
        })
        if snaps:
            for s in snaps:
                running_total += _UPTIME_WEIGHT.get(s["status"], 0.0)
            running_weight += len(snaps)

    uptime_pct = round(100.0 * running_total / running_weight, 4) if running_weight else 100.0
    # One snapshot ≈ one minute (the cron ticks every minute). Downtime
    # minutes is the gap between observed and full uptime.
    total_minutes = running_weight
    downtime_minutes = max(0, running_weight - int(round(running_total)))

    # Count incidents that overlapped this window.
    since_ts, _ = _day_bounds_utc(_today_utc() - _dt.timedelta(days=n - 1))
    all_incidents = status_db.list_recent_incidents(limit=500)
    incidents_in_window = [
        i for i in all_incidents
        if i["created_at"] >= since_ts and component in i["affected_components"]
    ]

    return {
        "component": component,
        "uptime_pct": uptime_pct,
        "total_minutes": total_minutes,
        "downtime_minutes": downtime_minutes,
        "incidents": len(incidents_in_window),
        "daily_data": daily_data,
    }


def compute_overall_uptime_last_n_days(n: int = 90, *, now: Optional[int] = None) -> dict:
    """Aggregate uptime across all components for the summary banner."""
    per_component = {
        k: compute_uptime_last_n_days(k, n, now=now) for k in COMPONENT_KEYS
    }

    total_minutes = sum(r["total_minutes"] for r in per_component.values())
    downtime_minutes = sum(r["downtime_minutes"] for r in per_component.values())
    uptime_pct = (
        round(100.0 * (total_minutes - downtime_minutes) / total_minutes, 4)
        if total_minutes > 0 else 100.0
    )

    # Daily rollup — each day's uptime is the mean of its per-component
    # uptimes (ignoring components with no data that day).
    daily_rollup: list[dict] = []
    # All components share the same day range; pull it from app arbitrarily.
    base_days = per_component[COMPONENT_KEYS[0]]["daily_data"]
    for idx, base in enumerate(base_days):
        day_pcts = [
            per_component[k]["daily_data"][idx]["uptime_pct"]
            for k in COMPONENT_KEYS
            if per_component[k]["daily_data"][idx]["uptime_pct"] is not None
        ]
        worst = "operational"
        for k in COMPONENT_KEYS:
            s = per_component[k]["daily_data"][idx]["status"]
            if s == "outage":
                worst = "outage"
                break
            if s == "degraded":
                worst = "degraded"
        mean_pct = round(sum(day_pcts) / len(day_pcts), 4) if day_pcts else None
        daily_rollup.append({
            "date": base["date"],
            "uptime_pct": mean_pct,
            "status": worst if day_pcts else "unknown",
        })

    incidents = len(
        {i["id"] for r in per_component.values() for i in status_db.list_recent_incidents(limit=500)
         if i["created_at"] >= _overall_window_start(n)
         and any(c in i["affected_components"] for c in COMPONENT_KEYS)}
    )

    return {
        "uptime_pct": uptime_pct,
        "total_minutes": total_minutes,
        "downtime_minutes": downtime_minutes,
        "incidents": incidents,
        "daily_rollup": daily_rollup,
        "per_component": per_component,
    }


def _overall_window_start(n_days: int) -> int:
    start_date = _today_utc() - _dt.timedelta(days=n_days - 1)
    start_ts, _ = _day_bounds_utc(start_date)
    return start_ts


def overall_system_status(now: Optional[int] = None) -> dict:
    """Instantaneous system state — the banner at the top of /status.

    Looks at the latest snapshot per component and the set of currently-
    open incidents. Returns:

        {
          "status": "operational" | "degraded" | "outage",
          "message": "All Systems Operational" | "Degraded performance" | "Service outage",
          "last_checked_ts": int | None,
          "components": {"app": {"status": ..., "response_time_ms": ..., "checked_at": ...}, ...}
        }
    """
    per_comp: dict[str, dict] = {}
    worst = "operational"
    latest_ts = 0

    for k in COMPONENT_KEYS:
        snap = status_db.get_latest_snapshot(k)
        if snap is None:
            per_comp[k] = {
                "status": "operational",
                "response_time_ms": None,
                "checked_at": None,
            }
            continue
        per_comp[k] = {
            "status": snap["status"],
            "response_time_ms": snap["response_time_ms"],
            "checked_at": snap["timestamp"],
        }
        if snap["timestamp"] > latest_ts:
            latest_ts = snap["timestamp"]
        if snap["status"] == "outage":
            worst = "outage"
        elif snap["status"] == "degraded" and worst != "outage":
            worst = "degraded"

    # If any incident is currently open, floor the status at "degraded"
    # even if every component is pinging green — someone set the
    # incident manually and we shouldn't override their judgement.
    open_incidents = status_db.list_open_incidents()
    if open_incidents and worst == "operational":
        worst = "degraded"

    messages = {
        "operational": "All Systems Operational",
        "degraded": "Some Systems Degraded",
        "outage": "Major Service Outage",
    }

    return {
        "status": worst,
        "message": messages[worst],
        "last_checked_ts": latest_ts or None,
        "components": per_comp,
        "open_incident_count": len(open_incidents),
    }
