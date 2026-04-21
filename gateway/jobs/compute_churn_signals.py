"""Nightly churn-risk computation.

For every user who has an active-or-cancelled subscription (tier != 'none'),
derive a risk score from the ``engagement_events`` table and upsert into
``churn_signals``. The /api/engagement/prompt endpoint and /admin/churn
dashboard read from there.

Risk formula (matches the product spec, deliberately simple so it's
auditable and the dashboard can explain each component):

  * +0.3 if last_active > 7 days ago
  * +0.5 if last_active > 14 days ago
  * +0.3 if recent_7d < 0.3 × prior_7d   (engagement declining fast)
  * +0.2 if no 'prediction_made' or 'intelligence_query' in last 30d
                                         (only passive usage)
  * -0.2 if any 'prediction_made' in last 7d
                                         (active behaviour)
  * Clamped to [0.0, 1.0]

Tier bucketing (also in the spec):
  * < 0.3 → healthy
  * 0.3 – 0.7 → at_risk
  * > 0.7 → critical

Engagement trend label (human-readable, stored redundantly on the row
so the admin page doesn't replay this logic in SQL):

  * dormant      — zero events in the last 14 days
  * declining    — recent_7d < 0.5 × prior_7d
  * rising       — recent_7d > 1.5 × prior_7d
  * stable       — otherwise

Scheduled nightly at 04:17 UTC (avoids the 04:00 report-generation job
window without adding another peak at the top of the hour).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.churn")


# Shared threshold constants so the admin dashboard can render the same
# cut-offs the job uses without drifting.
HEALTHY_MAX = 0.3
AT_RISK_MAX = 0.7


def _classify_tier(score: float) -> str:
    if score < HEALTHY_MAX:
        return "healthy"
    if score <= AT_RISK_MAX:
        return "at_risk"
    return "critical"


def _classify_trend(recent_7d: int, prior_7d: int, days_since_active: int | None) -> str:
    if days_since_active is None or days_since_active > 14:
        return "dormant"
    if prior_7d == 0 and recent_7d == 0:
        return "dormant"
    if prior_7d == 0:
        # No prior baseline but recent activity — treat as rising.
        return "rising"
    ratio = recent_7d / prior_7d
    if ratio < 0.5:
        return "declining"
    if ratio > 1.5:
        return "rising"
    return "stable"


def _compute_for_user(c, user_id: int, now_ts: int) -> dict[str, Any]:
    """Return the churn_signals row shape for this user, derived entirely
    from engagement_events. Pure function + SQLite reads; no writes."""
    # Aggregate the 30-day window in a single pass — cheaper than 4 queries.
    row = c.execute(
        """
        SELECT
          MAX(CASE WHEN event_type = 'login' THEN created_at END) AS last_login_at,
          MAX(created_at) AS last_active_at,
          SUM(CASE
                WHEN created_at >= datetime(?, 'unixepoch', '-7 days')
                THEN 1 ELSE 0 END) AS recent_7d,
          SUM(CASE
                WHEN created_at >= datetime(?, 'unixepoch', '-14 days')
                 AND created_at <  datetime(?, 'unixepoch', '-7 days')
                THEN 1 ELSE 0 END) AS prior_7d,
          SUM(CASE
                WHEN event_type IN ('prediction_made', 'intelligence_query')
                 AND created_at >= datetime(?, 'unixepoch', '-30 days')
                THEN 1 ELSE 0 END) AS active_30d,
          SUM(CASE
                WHEN event_type = 'prediction_made'
                 AND created_at >= datetime(?, 'unixepoch', '-7 days')
                THEN 1 ELSE 0 END) AS active_7d
        FROM engagement_events
        WHERE user_id = ?
          AND created_at >= datetime(?, 'unixepoch', '-30 days')
        """,
        (now_ts, now_ts, now_ts, now_ts, now_ts, user_id, now_ts),
    ).fetchone()

    last_login_at = row["last_login_at"] if row else None
    last_active_at = row["last_active_at"] if row else None
    recent_7d = int(row["recent_7d"] or 0) if row else 0
    prior_7d = int(row["prior_7d"] or 0) if row else 0
    active_30d = int(row["active_30d"] or 0) if row else 0
    active_7d = int(row["active_7d"] or 0) if row else 0

    days_since: int | None = None
    if last_active_at:
        delta_row = c.execute(
            "SELECT CAST((? - strftime('%s', ?)) / 86400 AS INTEGER) AS d",
            (now_ts, last_active_at),
        ).fetchone()
        days_since = max(0, int(delta_row["d"] if delta_row else 0))

    score = 0.0
    if days_since is None:
        # No activity at all in the 30d window: treat as maximally stale.
        # This is stronger than the +0.5 gate because there's no floor
        # to anchor against.
        score += 0.8
    else:
        if days_since > 7:
            score += 0.3
        if days_since > 14:
            score += 0.5  # cumulative with the >7d bump
    if prior_7d > 0 and recent_7d < 0.3 * prior_7d:
        score += 0.3
    if active_30d == 0:
        score += 0.2
    if active_7d > 0:
        score -= 0.2
    score = max(0.0, min(1.0, score))

    return {
        "user_id": user_id,
        "last_login_at": last_login_at,
        "last_active_at": last_active_at,
        "days_since_last_active": days_since,
        "recent_7d_events": recent_7d,
        "prior_7d_events": prior_7d,
        "engagement_trend": _classify_trend(recent_7d, prior_7d, days_since),
        "risk_score": score,
        "risk_tier": _classify_tier(score),
    }


def _subscriber_user_ids(c) -> list[int]:
    """Return user_ids for everyone with a non-'none' subscription tier.

    We include cancelled-but-in-window users because those are exactly
    the people the win-back flows care about. Excludes users with no
    ``__plan__`` row at all (free tier).
    """
    rows = c.execute(
        """
        SELECT DISTINCT s.user_id
        FROM subscriptions s
        WHERE s.dashboard_key = '__plan__'
          AND s.plan IS NOT NULL
          AND s.plan != ''
          AND s.plan != 'none'
        """
    ).fetchall()
    return [int(r["user_id"]) for r in rows]


def _upsert_signal(c, signal: dict[str, Any], now_ts: int) -> None:
    # sqlite UPSERT — single statement so we atomically either insert
    # or replace without a race window.
    c.execute(
        """
        INSERT INTO churn_signals
          (user_id, last_login_at, last_active_at, days_since_last_active,
           recent_7d_events, prior_7d_events, engagement_trend,
           risk_score, risk_tier, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))
        ON CONFLICT(user_id) DO UPDATE SET
          last_login_at = excluded.last_login_at,
          last_active_at = excluded.last_active_at,
          days_since_last_active = excluded.days_since_last_active,
          recent_7d_events = excluded.recent_7d_events,
          prior_7d_events = excluded.prior_7d_events,
          engagement_trend = excluded.engagement_trend,
          risk_score = excluded.risk_score,
          risk_tier = excluded.risk_tier,
          computed_at = excluded.computed_at
        """,
        (
            signal["user_id"],
            signal["last_login_at"],
            signal["last_active_at"],
            signal["days_since_last_active"],
            signal["recent_7d_events"],
            signal["prior_7d_events"],
            signal["engagement_trend"],
            signal["risk_score"],
            signal["risk_tier"],
            now_ts,
        ),
    )


def compute_churn_signals_sync(now_ts: int | None = None) -> dict[str, int]:
    """Synchronous core — importable by tests without needing the async
    backend. Returns {"total": N, "healthy": …, "at_risk": …, "critical": …}.
    """
    import db

    now_ts = int(now_ts if now_ts is not None else time.time())
    counts = {"total": 0, "healthy": 0, "at_risk": 0, "critical": 0}

    with db.conn() as c:
        user_ids = _subscriber_user_ids(c)
        for uid in user_ids:
            signal = _compute_for_user(c, uid, now_ts)
            _upsert_signal(c, signal, now_ts)
            counts["total"] += 1
            counts[signal["risk_tier"]] = counts.get(signal["risk_tier"], 0) + 1
    return counts


@register_job("compute_churn_signals")
async def compute_churn_signals(ctx=None) -> dict[str, int]:
    """Background-queue wrapper for compute_churn_signals_sync."""
    result = compute_churn_signals_sync()
    log.info("churn signals recomputed: %s", result)
    return result


# Nightly at 04:17 UTC. Stagger off the top of the hour where report
# generation runs — keeps the SQLite writer from queueing with the
# weekly-report job.
register_cron("compute_churn_signals", hour=4, minute=17)
