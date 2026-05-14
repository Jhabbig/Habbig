"""Search-analytics queries — feeds /admin/search-analytics.

Lives under queries/ alongside ``sharing_metrics`` and ``performance``
(the established home for admin-dashboard SQL). The route handler in
``search_routes.py`` calls these helpers and hands their return values
straight to the template — no business logic, no side effects.

Contract: every function returns plain dicts / lists of dicts ready
for JSON dump. Read-only by design; the write path lives in
``search_routes._log_query`` + the click UPDATE.

Window everywhere is "trailing N days from now()". Default 7 days
matches the legacy inline queries that lived in the route handler.

Schema reference (migration 117):
  search_queries(
    id, user_id, query, result_count,
    clicked_result_type, clicked_result_id, clicked_at, ts
  )

Conversion funnel definition — search → click → save → subscribe:
  * search    — user_id is non-null on any row in window
  * click     — same user has any row with clicked_at NOT NULL
  * save      — same user has any saved_predictions row created
                AFTER their first search in the window
  * subscribe — same user has any subscriptions row started AFTER
                their first search in the window
Anonymous searches are excluded from the funnel (user_id IS NULL)
because we have no way to attribute downstream actions to them.
"""

from __future__ import annotations

import time
from typing import Any

import db


# ── Internal helpers ─────────────────────────────────────────────────


def _cutoff(window_days: int) -> int:
    """Unix-second cutoff for "trailing N days from now"."""
    return int(time.time()) - max(1, int(window_days)) * 86400


def _clamp_limit(limit: int, hard_max: int = 200) -> int:
    """Defence-in-depth: route handlers shouldn't pass huge limits, but
    if they do we clamp here so a typo can't pull 100k rows."""
    return max(1, min(int(limit), hard_max))


# ── Top queries ──────────────────────────────────────────────────────


def top_queries(window_days: int = 7, limit: int = 50) -> list[dict]:
    """Top queries by hit count over the window.

    Returns one row per distinct query string:
      {query, hits, unique_users, last_searched, avg_results, clicks}

    ``unique_users`` counts distinct non-null user_id only — anon hits
    don't inflate the figure. ``last_searched`` is the most recent ``ts``
    for that query (unix seconds), useful for "freshly trending" reads.
    """
    cutoff = _cutoff(window_days)
    n = _clamp_limit(limit)
    with db.conn() as c:
        rows = c.execute(
            "SELECT query, "
            "       COUNT(*) AS hits, "
            "       COUNT(DISTINCT user_id) AS unique_users, "
            "       MAX(ts) AS last_searched, "
            "       AVG(result_count) AS avg_results, "
            "       SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicks "
            "FROM search_queries "
            "WHERE ts >= ? "
            "GROUP BY query "
            "ORDER BY hits DESC, query ASC "
            "LIMIT ?",
            (cutoff, n),
        ).fetchall()
    return [
        {
            "query": r["query"],
            "hits": int(r["hits"] or 0),
            "unique_users": int(r["unique_users"] or 0),
            "last_searched": int(r["last_searched"] or 0),
            "avg_results": float(r["avg_results"] or 0.0),
            "clicks": int(r["clicks"] or 0),
        }
        for r in rows
    ]


# ── No-result queries ────────────────────────────────────────────────


def no_result_queries(window_days: int = 7, limit: int = 50) -> list[dict]:
    """Queries that returned zero hits — content-gap signals.

    Same shape as ``top_queries`` minus the avg_results/clicks columns
    (both are uninteresting when the result set is empty). Excludes
    queries shorter than 2 chars to filter the natural noise from
    palette open-and-discard (the route already drops <2 chars before
    logging, but defence-in-depth).
    """
    cutoff = _cutoff(window_days)
    n = _clamp_limit(limit)
    with db.conn() as c:
        rows = c.execute(
            "SELECT query, "
            "       COUNT(*) AS hits, "
            "       COUNT(DISTINCT user_id) AS unique_users, "
            "       MAX(ts) AS last_searched "
            "FROM search_queries "
            "WHERE ts >= ? AND result_count = 0 AND LENGTH(query) >= 2 "
            "GROUP BY query "
            "ORDER BY hits DESC, query ASC "
            "LIMIT ?",
            (cutoff, n),
        ).fetchall()
    return [
        {
            "query": r["query"],
            "hits": int(r["hits"] or 0),
            "unique_users": int(r["unique_users"] or 0),
            "last_searched": int(r["last_searched"] or 0),
        }
        for r in rows
    ]


# ── Conversion funnel ────────────────────────────────────────────────


def query_to_conversion_rate(window_days: int = 7) -> dict[str, Any]:
    """Conversion funnel: search → click → save → subscribe.

    Counts distinct *users* (not events) at each stage. Anonymous
    searches are excluded — without a user_id we can't link a search
    to a later save or subscribe.

    Each downstream step is gated on having happened AFTER the user's
    first search in the window, so we don't credit a save / subscribe
    that pre-dates the search activity. This is the conservative
    direction: a user who saved before they searched and never saved
    again will count as searched-but-didn't-save.

    Returns:
        {
            "window_days": int,
            "searched": int,
            "clicked": int,
            "saved": int,
            "subscribed": int,
            "click_rate": float,        # clicked / searched
            "save_rate": float,         # saved / searched
            "subscribe_rate": float,    # subscribed / searched
        }
    """
    cutoff = _cutoff(window_days)
    out: dict[str, Any] = {
        "window_days": int(window_days),
        "searched": 0,
        "clicked": 0,
        "saved": 0,
        "subscribed": 0,
        "click_rate": 0.0,
        "save_rate": 0.0,
        "subscribe_rate": 0.0,
    }
    with db.conn() as c:
        # Stage 1: distinct users who searched (non-null user_id only)
        row = c.execute(
            "SELECT COUNT(DISTINCT user_id) AS n "
            "FROM search_queries "
            "WHERE ts >= ? AND user_id IS NOT NULL",
            (cutoff,),
        ).fetchone()
        searched = int(row["n"] or 0) if row else 0
        out["searched"] = searched

        if searched == 0:
            return out

        # Stage 2: those users who also have any click in the window
        row = c.execute(
            "SELECT COUNT(DISTINCT user_id) AS n "
            "FROM search_queries "
            "WHERE ts >= ? AND user_id IS NOT NULL "
            "  AND clicked_at IS NOT NULL",
            (cutoff,),
        ).fetchone()
        out["clicked"] = int(row["n"] or 0) if row else 0

        # Stage 3: those users whose saved_predictions.saved_at is after
        # their first search in the window. EXISTS subquery on per-user
        # min(ts) so a user who saved months ago and never saved again
        # doesn't count.
        # Wrap in try/except: saved_predictions table is part of the
        # core schema (db.init_db) but if a degraded test harness skips
        # it we should still return the searched/clicked figures rather
        # than 500.
        try:
            row = c.execute(
                "SELECT COUNT(DISTINCT sq.user_id) AS n "
                "FROM search_queries sq "
                "WHERE sq.ts >= ? AND sq.user_id IS NOT NULL "
                "  AND EXISTS ( "
                "    SELECT 1 FROM saved_predictions sp "
                "    WHERE sp.user_id = sq.user_id "
                "      AND sp.saved_at >= ( "
                "        SELECT MIN(ts) FROM search_queries "
                "        WHERE user_id = sq.user_id AND ts >= ? "
                "      ) "
                "  )",
                (cutoff, cutoff),
            ).fetchone()
            out["saved"] = int(row["n"] or 0) if row else 0
        except Exception:
            out["saved"] = 0

        # Stage 4: subscriptions.started_at after the user's first
        # search in the window.
        try:
            row = c.execute(
                "SELECT COUNT(DISTINCT sq.user_id) AS n "
                "FROM search_queries sq "
                "WHERE sq.ts >= ? AND sq.user_id IS NOT NULL "
                "  AND EXISTS ( "
                "    SELECT 1 FROM subscriptions s "
                "    WHERE s.user_id = sq.user_id "
                "      AND s.started_at >= ( "
                "        SELECT MIN(ts) FROM search_queries "
                "        WHERE user_id = sq.user_id AND ts >= ? "
                "      ) "
                "  )",
                (cutoff, cutoff),
            ).fetchone()
            out["subscribed"] = int(row["n"] or 0) if row else 0
        except Exception:
            out["subscribed"] = 0

    out["click_rate"] = out["clicked"] / searched
    out["save_rate"] = out["saved"] / searched
    out["subscribe_rate"] = out["subscribed"] / searched
    return out


# ── Time-of-day distribution ─────────────────────────────────────────


def hourly_distribution(window_days: int = 7) -> list[dict]:
    """Counts grouped by hour-of-day (0–23), aggregated across the
    window. Returns exactly 24 rows, even for hours with zero hits,
    so the dashboard can render a stable 24-bar chart.

    Hour is computed in UTC — the analytics page does not pretend to
    know the viewer's timezone. The admin-page caption notes "UTC".
    """
    cutoff = _cutoff(window_days)
    counts = [0] * 24
    with db.conn() as c:
        rows = c.execute(
            "SELECT CAST(strftime('%H', ts, 'unixepoch') AS INTEGER) AS hour, "
            "       COUNT(*) AS n "
            "FROM search_queries "
            "WHERE ts >= ? "
            "GROUP BY hour",
            (cutoff,),
        ).fetchall()
    for r in rows:
        h = int(r["hour"] or 0)
        if 0 <= h <= 23:
            counts[h] = int(r["n"] or 0)
    return [{"hour": h, "hits": counts[h]} for h in range(24)]


__all__ = (
    "top_queries",
    "no_result_queries",
    "query_to_conversion_rate",
    "hourly_distribution",
)
