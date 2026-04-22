"""Admin-dashboard data accessors for the share surface.

Lives under queries/ (the established home for admin-panel SQL — see
sibling ``performance.py``). The /admin/sharing route (owned by the
admin_routes.py author) will call these helpers and hand their return
values to a template.

Contract: every function returns plain dicts / lists of dicts, ready
for JSON dump. No side effects. No write paths — admin views are
strictly read-only from this module.

Columns returned match the dashboard layout spec:

  * totals_by_type(days)       — one row per share_type
  * top_shared_markets         — grouped by market_slug, ranked by views
  * top_shared_sources         — same shape, by source_handle
  * top_sharers                — user_id + username + conversions
  * conversion_rates_by_type   — signups / views per type
  * referrer_breakdown         — count by bucketed referrer string
  * country_breakdown          — count by CF-IPCountry
"""

from __future__ import annotations

import time
from typing import Optional

import db


# ── Internal helpers ────────────────────────────────────────────────


def _cutoff(days: int) -> int:
    return int(time.time()) - days * 86400


_VALID_TYPES: tuple[str, ...] = ("market", "source", "prediction")


# ── Totals / overview ───────────────────────────────────────────────


def totals_by_type(days: int = 30) -> list[dict]:
    """One row per share_type with counts over the window.

    Returns every valid type even if it has zero rows — the admin UI
    prefers stable shape across periods so a type disappearing
    between 'last 7 days' and 'last 30' doesn't shift the layout."""
    cutoff = _cutoff(days)
    out: list[dict] = []
    with db.conn() as c:
        for st in _VALID_TYPES:
            row = c.execute(
                "SELECT COUNT(*) AS views, "
                "       SUM(CASE WHEN signed_up = 1 THEN 1 ELSE 0 END) AS conversions "
                "FROM share_metrics "
                "WHERE share_type = ? AND viewed_at >= ?",
                (st, cutoff),
            ).fetchone()
            views = int(row["views"] or 0)
            conversions = int(row["conversions"] or 0)
            conv_rate = round(100.0 * conversions / views, 1) if views else 0.0
            out.append({
                "share_type": st,
                "views": views,
                "conversions": conversions,
                "conversion_rate_pct": conv_rate,
            })
    return out


def overall_stats(days: int = 30) -> dict:
    """Top-of-page summary card. One query, one row."""
    cutoff = _cutoff(days)
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS views, "
            "       SUM(CASE WHEN signed_up = 1 THEN 1 ELSE 0 END) AS conversions, "
            "       COUNT(DISTINCT viewer_country) AS countries "
            "FROM share_metrics WHERE viewed_at >= ?",
            (cutoff,),
        ).fetchone()
    views = int(row["views"] or 0)
    conversions = int(row["conversions"] or 0)
    return {
        "window_days": days,
        "total_views": views,
        "total_conversions": conversions,
        "conversion_rate_pct": round(100.0 * conversions / views, 1) if views else 0.0,
        "distinct_countries": int(row["countries"] or 0),
    }


# ── Top-N lists ─────────────────────────────────────────────────────


def top_shared_markets(days: int = 30, limit: int = 20) -> list[dict]:
    """Rank markets by total viewers of their share cards. Joins back
    into ``shared_market_cards`` to collapse every token on the same
    market under one row — the admin surface wants "which markets are
    people sharing", not "which tokens".

    The subquery grabs distinct market_slugs for each share_metrics
    row; we then GROUP BY slug to sum across all tokens. Single
    scan per window with the idx_share_metrics_type_time + index-only
    join on shared_market_cards.id."""
    cutoff = _cutoff(days)
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT smc.market_slug              AS market_slug,
                   COUNT(*)                     AS views,
                   COUNT(DISTINCT smc.id)       AS distinct_shares,
                   SUM(CASE WHEN sm.signed_up = 1 THEN 1 ELSE 0 END) AS conversions
              FROM share_metrics sm
              JOIN shared_market_cards smc ON smc.id = sm.share_id
             WHERE sm.share_type = 'market' AND sm.viewed_at >= ?
             GROUP BY smc.market_slug
             ORDER BY views DESC
             LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return [
        {
            "market_slug": r["market_slug"],
            "views": int(r["views"]),
            "distinct_shares": int(r["distinct_shares"]),
            "conversions": int(r["conversions"] or 0),
        } for r in rows
    ]


def top_shared_sources(days: int = 30, limit: int = 20) -> list[dict]:
    """Same shape as top_shared_markets, keyed by source_handle."""
    cutoff = _cutoff(days)
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT ssc.source_handle            AS source_handle,
                   COUNT(*)                     AS views,
                   COUNT(DISTINCT ssc.id)       AS distinct_shares,
                   SUM(CASE WHEN sm.signed_up = 1 THEN 1 ELSE 0 END) AS conversions
              FROM share_metrics sm
              JOIN shared_source_cards ssc ON ssc.id = sm.share_id
             WHERE sm.share_type = 'source' AND sm.viewed_at >= ?
             GROUP BY ssc.source_handle
             ORDER BY views DESC
             LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return [
        {
            "source_handle": r["source_handle"],
            "views": int(r["views"]),
            "distinct_shares": int(r["distinct_shares"]),
            "conversions": int(r["conversions"] or 0),
        } for r in rows
    ]


def top_sharers(days: int = 30, limit: int = 20) -> list[dict]:
    """Users ranked by signups attributed to their shares. A UNION
    across the three share tables joins every signed-up view back to
    the originating sharer, then GROUP BY user_id."""
    cutoff = _cutoff(days)
    with db.conn() as c:
        rows = c.execute(
            """
            WITH attributed AS (
                SELECT smc.sharer_user_id AS user_id
                  FROM share_metrics sm
                  JOIN shared_market_cards smc ON smc.id = sm.share_id
                 WHERE sm.share_type = 'market'
                   AND sm.signed_up = 1
                   AND sm.viewed_at >= ?
                UNION ALL
                SELECT ssc.sharer_user_id AS user_id
                  FROM share_metrics sm
                  JOIN shared_source_cards ssc ON ssc.id = sm.share_id
                 WHERE sm.share_type = 'source'
                   AND sm.signed_up = 1
                   AND sm.viewed_at >= ?
                UNION ALL
                SELECT sp.sharer_user_id AS user_id
                  FROM share_metrics sm
                  JOIN shared_predictions sp ON sp.id = sm.share_id
                 WHERE sm.share_type = 'prediction'
                   AND sm.signed_up = 1
                   AND sm.viewed_at >= ?
            )
            SELECT u.id AS user_id, u.username, u.email,
                   COUNT(*) AS conversions
              FROM attributed a
              JOIN users u ON u.id = a.user_id
             GROUP BY u.id
             ORDER BY conversions DESC
             LIMIT ?
            """,
            (cutoff, cutoff, cutoff, limit),
        ).fetchall()
    return [
        {
            "user_id": int(r["user_id"]),
            "username": r["username"],
            # Email is admin-only data — the page uses it for contact
            # but shouldn't render it to non-admins. No redaction here;
            # this module ships data to the route, the route checks auth.
            "email": r["email"],
            "conversions": int(r["conversions"]),
        } for r in rows
    ]


# ── Distribution breakdowns ─────────────────────────────────────────


def referrer_breakdown(days: int = 30) -> list[dict]:
    """Count by coarse referrer bucket (twitter/linkedin/slack/…)."""
    cutoff = _cutoff(days)
    with db.conn() as c:
        rows = c.execute(
            "SELECT COALESCE(referrer, 'direct') AS referrer, "
            "       COUNT(*) AS views, "
            "       SUM(CASE WHEN signed_up = 1 THEN 1 ELSE 0 END) AS conversions "
            "FROM share_metrics WHERE viewed_at >= ? "
            "GROUP BY referrer ORDER BY views DESC",
            (cutoff,),
        ).fetchall()
    return [
        {
            "referrer": r["referrer"],
            "views": int(r["views"]),
            "conversions": int(r["conversions"] or 0),
        } for r in rows
    ]


def country_breakdown(days: int = 30, limit: int = 20) -> list[dict]:
    """Top N countries by view count. NULL (no CF header) grouped as
    ``unknown`` so the dashboard shows total reach without silent
    drops."""
    cutoff = _cutoff(days)
    with db.conn() as c:
        rows = c.execute(
            "SELECT COALESCE(viewer_country, 'unknown') AS country, "
            "       COUNT(*) AS views, "
            "       SUM(CASE WHEN signed_up = 1 THEN 1 ELSE 0 END) AS conversions "
            "FROM share_metrics WHERE viewed_at >= ? "
            "GROUP BY country ORDER BY views DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    return [
        {
            "country": r["country"],
            "views": int(r["views"]),
            "conversions": int(r["conversions"] or 0),
        } for r in rows
    ]


def daily_timeseries(days: int = 30) -> list[dict]:
    """Per-day view count bucketed by share_type for the chart.
    Returns a sorted list of dicts {date_yyyymmdd, market, source,
    prediction, total} — dense (every day in the window, zeros
    included) so the chart renders with no gaps."""
    cutoff = _cutoff(days)
    # Pull raw counts grouped by (day, type), then zip into dense days.
    with db.conn() as c:
        rows = c.execute(
            "SELECT CAST(strftime('%Y%m%d', viewed_at, 'unixepoch') AS INTEGER) AS day, "
            "       share_type, COUNT(*) AS n "
            "FROM share_metrics WHERE viewed_at >= ? "
            "GROUP BY day, share_type ORDER BY day",
            (cutoff,),
        ).fetchall()
    by_day: dict[int, dict] = {}
    for r in rows:
        d = int(r["day"])
        bucket = by_day.setdefault(
            d, {"date_yyyymmdd": d, "market": 0, "source": 0, "prediction": 0, "total": 0}
        )
        bucket[r["share_type"]] = int(r["n"])
        bucket["total"] += int(r["n"])
    # Dense fill using Python date arithmetic rather than SQL —
    # sqlite's date functions get awkward around month boundaries.
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).date()
    out: list[dict] = []
    for offset in range(days - 1, -1, -1):
        d = today - timedelta(days=offset)
        key = d.year * 10000 + d.month * 100 + d.day
        out.append(by_day.get(key, {
            "date_yyyymmdd": key, "market": 0, "source": 0,
            "prediction": 0, "total": 0,
        }))
    return out
