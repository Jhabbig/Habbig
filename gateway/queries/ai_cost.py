"""Queries for /admin/cost-alerts — Claude API spend monitoring.

Surfaces five primitives the admin page (and the JSON refresh endpoint)
need to render the dashboard:

  - :func:`get_total_cost` — sum of ``cost_usd`` over a rolling window.
  - :func:`get_total_cost_mtd` — month-to-date total (calendar UTC).
  - :func:`get_per_feature_costs` — call counts + cost grouped by
    feature, returned with average cost-per-call pre-computed so the
    template stays declarative.
  - :func:`get_daily_costs` — last 30 calendar days as a list of
    ``{day, cost_usd}`` rows. Used for the monochrome bar chart.
  - :func:`list_cost_alerts` — most recent rows from
    ``claude_cost_alerts``.
  - :func:`get_kill_switch_status` / :func:`set_kill_switch` — thin
    wrappers around ``ai.client`` so the route handler doesn't reach
    across packages.

All functions tolerate the table being empty or absent (early bootstrap,
fresh dev DBs) — they return a defensible default rather than raising.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import time
from typing import Any, Optional

import db


log = logging.getLogger("queries.ai_cost")


# ── Window helpers ─────────────────────────────────────────────────────


def _window_start(window_hours: int) -> int:
    """Return the unix-second start of a rolling ``window_hours`` window."""
    return int(time.time()) - max(1, int(window_hours)) * 3600


def _month_to_date_start() -> int:
    """First-of-month 00:00 UTC as a unix-second."""
    now = _dt.datetime.utcnow()
    first = _dt.datetime(now.year, now.month, 1, tzinfo=_dt.timezone.utc)
    return int(first.timestamp())


# ── Spend totals ───────────────────────────────────────────────────────


def get_total_cost(window_hours: int = 24) -> float:
    """Sum ``cost_usd`` across ``claude_usage_log`` for the trailing window.

    Returns 0.0 if the table is empty or missing.
    """
    start = _window_start(window_hours)
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total "
                "FROM claude_usage_log WHERE timestamp >= ?",
                (start,),
            ).fetchone()
    except sqlite3.Error:
        return 0.0
    if not row:
        return 0.0
    try:
        return round(float(row["total"] or 0.0), 4)
    except (KeyError, TypeError, ValueError):
        return 0.0


def get_total_cost_mtd() -> float:
    """Month-to-date spend in USD. UTC calendar bounds."""
    start = _month_to_date_start()
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total "
                "FROM claude_usage_log WHERE timestamp >= ?",
                (start,),
            ).fetchone()
    except sqlite3.Error:
        return 0.0
    if not row:
        return 0.0
    try:
        return round(float(row["total"] or 0.0), 4)
    except (KeyError, TypeError, ValueError):
        return 0.0


# ── Per-feature breakdown ──────────────────────────────────────────────


def get_per_feature_costs(window_hours: int = 24) -> list[dict[str, Any]]:
    """Per-feature rollup for the trailing window.

    Returns a list of dicts with keys ``feature``, ``calls``, ``cost_usd``,
    ``avg_cost_per_call`` sorted by ``cost_usd`` descending. Cache hits
    are included in ``calls`` (they're rows in the log) but contribute
    $0 cost — matches the existing /admin/ai-usage semantics.
    """
    start = _window_start(window_hours)
    try:
        with db.conn() as c:
            rows = c.execute(
                """
                SELECT feature,
                       COUNT(*) AS calls,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd
                FROM claude_usage_log
                WHERE timestamp >= ?
                GROUP BY feature
                ORDER BY cost_usd DESC, calls DESC
                """,
                (start,),
            ).fetchall()
    except sqlite3.Error:
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        calls = int(r["calls"] or 0)
        cost = float(r["cost_usd"] or 0.0)
        avg = round(cost / calls, 6) if calls else 0.0
        out.append({
            "feature": r["feature"] or "(unknown)",
            "calls": calls,
            "cost_usd": round(cost, 4),
            "avg_cost_per_call": avg,
        })
    return out


# ── Daily series (for the bar chart) ───────────────────────────────────


def get_daily_costs(days: int = 30) -> list[dict[str, Any]]:
    """Last ``days`` UTC calendar days of cost totals.

    Days with zero spend are still included (cost_usd=0) so the bar
    chart renders an even x-axis even when the platform is idle.
    Earliest day first so the chart reads left-to-right.
    """
    days = max(1, min(180, int(days)))
    start_ts = int(time.time()) - days * 86400
    try:
        with db.conn() as c:
            rows = c.execute(
                """
                SELECT strftime('%Y-%m-%d', timestamp, 'unixepoch') AS day,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd,
                       COUNT(*) AS calls
                FROM claude_usage_log
                WHERE timestamp >= ?
                GROUP BY day
                ORDER BY day ASC
                """,
                (start_ts,),
            ).fetchall()
    except sqlite3.Error:
        rows = []

    by_day = {
        r["day"]: {
            "cost_usd": round(float(r["cost_usd"] or 0.0), 4),
            "calls": int(r["calls"] or 0),
        }
        for r in rows
    }

    today = _dt.datetime.utcnow().date()
    out: list[dict[str, Any]] = []
    for i in range(days - 1, -1, -1):
        d = today - _dt.timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        entry = by_day.get(key) or {"cost_usd": 0.0, "calls": 0}
        out.append({"day": key, **entry})
    return out


# ── Alert log ──────────────────────────────────────────────────────────


def list_cost_alerts(limit: int = 50) -> list[dict[str, Any]]:
    """Most recent rows from ``claude_cost_alerts``.

    Sorted newest first by ``sent_at`` (falls back to ``alert_date``
    ordering when ``sent_at`` is NULL — e.g. seeded rows in tests).
    Returns an empty list on missing table or any sqlite error.
    """
    limit = max(1, min(500, int(limit)))
    try:
        with db.conn() as c:
            rows = c.execute(
                """
                SELECT id, alert_date, threshold_usd, total_cost_usd, sent_at
                FROM claude_cost_alerts
                ORDER BY COALESCE(sent_at, 0) DESC, alert_date DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "id": int(r["id"]),
            "alert_date": r["alert_date"],
            "threshold_usd": float(r["threshold_usd"] or 0.0),
            "total_cost_usd": float(r["total_cost_usd"] or 0.0),
            "sent_at": int(r["sent_at"]) if r["sent_at"] is not None else None,
        }
        for r in rows
    ]


# ── Kill-switch passthrough ────────────────────────────────────────────
#
# The actual state lives in ai.client (single source of truth — call_claude
# reads it on every dispatch). We re-export thin wrappers here so the route
# handler doesn't need to import across packages.


def get_kill_switch_status() -> dict[str, Any]:
    """Current kill-switch state. See :func:`ai.client.get_kill_switch_status`."""
    try:
        from ai import client as _ai_client
        return _ai_client.get_kill_switch_status()
    except Exception:
        return {"active": False, "reason": None,
                "triggered_at": None, "triggered_by": None}


def set_kill_switch(*, active: bool, reason: Optional[str] = None,
                    triggered_by: Optional[str] = None) -> None:
    """Toggle the kill-switch. See :func:`ai.client.set_kill_switch`."""
    try:
        from ai import client as _ai_client
        _ai_client.set_kill_switch(
            active=active, reason=reason, triggered_by=triggered_by,
        )
    except Exception:
        log.exception("set_kill_switch passthrough failed")


__all__ = [
    "get_total_cost",
    "get_total_cost_mtd",
    "get_per_feature_costs",
    "get_daily_costs",
    "list_cost_alerts",
    "get_kill_switch_status",
    "set_kill_switch",
]
