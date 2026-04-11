"""Email jobs — every outbound email goes through here, never inline.

Routes and webhook handlers call `enqueue_email(...)` which places a
`send_email` job on the queue. The worker resolves the template, renders
it, and calls EmailService.send(). Failures retry up to 3x with backoff.
"""

from __future__ import annotations

import logging
from typing import Any

from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.email")


def _unsub_url(user_id: int, email: str, scope: str) -> str:
    try:
        from email_system.unsubscribe import UnsubscribeManager
        return UnsubscribeManager.get_unsubscribe_url(user_id, email, scope)
    except Exception:
        return ""


@register_job("send_email")
async def send_email_job(
    to: str,
    template: str,
    context: dict,
    reply_to: str | None = None,
    tags: list | None = None,
) -> dict[str, Any]:
    """Render `template` with `context` and send via EmailService.

    Raises on failure so the backend retries. The audit log records the
    exception string so the admin panel shows why it broke.
    """
    from email_system.service import EmailService  # lazy to avoid circular imports

    service = EmailService()
    success = await service.send_template(
        to=to,
        template=template,
        context=context,
        reply_to=reply_to,
        tags=tags,
    )
    if not success:
        raise RuntimeError(f"email send failed: to={to} template={template}")
    return {"sent": True, "to": to, "template": template}


async def enqueue_email(
    to: str,
    template: str,
    context: dict,
    reply_to: str | None = None,
    tags: list | None = None,
) -> int:
    """Convenience wrapper — call from routes instead of sending inline."""
    from jobs import enqueue_job
    return await enqueue_job(
        "send_email",
        to=to,
        template=template,
        context=context,
        reply_to=reply_to,
        tags=tags,
    )


@register_job("send_weekly_digest_batch")
async def send_weekly_digest_batch() -> dict[str, Any]:
    """Send the weekly digest to every opted-in active subscriber.

    Processes in batches of 50 users to keep memory flat. Each user's
    digest is enqueued as an individual `send_email` job so a single
    rendering failure doesn't kill the whole run.
    """
    import db
    import datetime as _dt
    import time as _time

    now = int(_time.time())
    week_start = now - 7 * 86400

    # Pull users who opted in and have an active plan.
    with db.conn() as c:
        users = c.execute(
            "SELECT u.id, u.email, u.username, u.email_digest, u.email_unsubscribed_at "
            "FROM users u "
            "WHERE COALESCE(u.email_digest, 1) = 1 "
            "AND u.email_unsubscribed_at IS NULL "
            "AND COALESCE(u.is_deleted, 0) = 0"
        ).fetchall()

    enqueued = 0
    skipped = 0
    for u in users:
        tier = db.get_user_subscription_tier(u["id"]) if hasattr(db, "get_user_subscription_tier") else "none"
        if tier == "none":
            skipped += 1
            continue

        # Top 5 high-EV predictions from the last week
        top_predictions: list[dict] = []
        try:
            preds = db.list_recent_predictions(limit=5)
            for p in preds:
                cred = p["global_credibility"] if "global_credibility" in p.keys() else None
                top_predictions.append({
                    "source": f"@{p['source_handle']}",
                    "content": (p["content"] or "")[:200],
                    "credibility": round(cred, 2) if cred is not None else None,
                    "category": p["category"],
                })
        except Exception:
            pass

        # Top 3 most accurate sources
        top_sources: list[dict] = []
        try:
            sources = db.list_all_source_credibilities() if hasattr(db, "list_all_source_credibilities") else []
            sources = sorted(
                [s for s in sources if s["total_predictions"] >= 5],
                key=lambda s: s["global_credibility"],
                reverse=True,
            )[:3]
            for s in sources:
                top_sources.append({
                    "handle": s["source_handle"],
                    "credibility": round(s["global_credibility"], 2),
                    "accuracy": (
                        f"{int(100 * s['correct_predictions'] / max(s['total_predictions'], 1))}%"
                    ),
                })
        except Exception:
            pass

        context = {
            "display_name": u["username"] or (u["email"] or "").split("@")[0],
            "week_start": _dt.datetime.fromtimestamp(week_start).strftime("%b %d"),
            "week_end": _dt.datetime.fromtimestamp(now).strftime("%b %d, %Y"),
            "top_predictions": top_predictions,
            "top_sources": top_sources,
            "unsubscribe_url": _unsub_url(u["id"], u["email"], "digest"),
        }
        await enqueue_email(
            to=u["email"],
            template="weekly_digest",
            context=context,
            tags=["digest"],
        )
        enqueued += 1

    return {"enqueued": enqueued, "skipped": skipped}


@register_job("send_morning_briefings")
async def send_morning_briefings() -> dict[str, Any]:
    """Send personalised morning intelligence briefing emails (F7).

    For each opted-in user:
      - Top 5 markets by |betyc_edge|
      - New predictions from followed sources (last 24h)
      - Markets approaching resolution
    """
    import db
    import os
    import time as _time
    from backend.markets import unified_markets
    from backend.markets.polymarket_client import PolymarketClient
    from backend.markets.kalshi_client import KalshiClient

    app_url = os.environ.get("APP_URL", "https://narve.ai")
    now = int(_time.time())
    yesterday = now - 86400

    # Get opted-in users
    with db.conn() as c:
        users = c.execute(
            "SELECT id, email, username FROM users "
            "WHERE morning_briefing_enabled = 1 AND COALESCE(is_deleted, 0) = 0 "
            "AND COALESCE(email_unsubscribed_at, 0) = 0"
        ).fetchall()

    if not users:
        return {"sent": 0, "reason": "no opted-in users"}

    # Fetch and enrich markets once for all users
    try:
        poly = PolymarketClient()
        kalshi = KalshiClient(
            base_url=os.environ.get("KALSHI_API_BASE", "https://trading-api.kalshi.com/trade-api/v2"),
        )
        markets = await unified_markets.fetch_unified_markets(poly, kalshi, cache_ttl=300)
        active = [m for m in markets if m.status == "active"]
        enriched = unified_markets.enrich_markets_with_intelligence(active)
        await poly.close()
        await kalshi.close()
    except Exception as e:
        log.exception("Morning briefing: market fetch failed: %s", e)
        return {"sent": 0, "error": str(e)}

    # Top 5 by absolute edge
    with_edge = [m for m in enriched if m.betyc_ev_score is not None and m.betyc_prediction_count >= 1]
    with_edge.sort(key=lambda m: abs(m.betyc_ev_score or 0), reverse=True)
    top_5 = with_edge[:5]

    # Approaching resolutions (close_time within 7 days)
    from datetime import datetime, timezone
    approaching = []
    for m in enriched:
        if m.close_time:
            try:
                close_dt = datetime.fromisoformat(m.close_time.replace("Z", "+00:00"))
                days_until = (close_dt - datetime.now(timezone.utc)).days
                if 0 <= days_until <= 7:
                    approaching.append({"title": m.title, "close_time": close_dt.strftime("%b %d")})
            except (ValueError, TypeError):
                pass
    approaching = approaching[:5]

    sent = 0
    for user in users:
        # New signals from followed sources (last 24h)
        with db.conn() as c:
            new_signals_rows = c.execute(
                "SELECT p.source_handle, p.content, sc.global_credibility "
                "FROM predictions p "
                "JOIN followed_sources fs ON fs.source_handle = p.source_handle AND fs.user_id = ? "
                "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
                "WHERE p.extracted_at >= ? "
                "ORDER BY p.extracted_at DESC LIMIT 5",
                (user["id"], yesterday),
            ).fetchall()

        new_signals = [
            {
                "source_handle": r["source_handle"],
                "content": (r["content"] or "")[:120],
                "credibility": round(r["global_credibility"] or 0.5, 2),
            }
            for r in new_signals_rows
        ]

        from datetime import date
        context = {
            "app_url": app_url,
            "date": date.today().strftime("%B %d, %Y"),
            "display_name": user["username"] or user["email"].split("@")[0],
            "top_edge_markets": [
                {
                    "title": m.title[:80],
                    "market_price": int(m.yes_price * 100),
                    "betyc_price": int((m.yes_price + (m.betyc_ev_score or 0)) * 100),
                    "edge": m.betyc_ev_score,
                    "edge_display": f"{'+' if m.betyc_ev_score > 0 else ''}{int(m.betyc_ev_score * 100)}pp",
                    "source_count": m.betyc_prediction_count,
                }
                for m in top_5
            ],
            "new_signals": new_signals,
            "approaching_resolutions": approaching,
            "unsubscribe_url": f"{app_url}/unsubscribe?type=digest",
        }

        try:
            await enqueue_email(
                to=user["email"],
                template="morning_briefing",
                context=context,
                tags=["morning_briefing"],
            )
            sent += 1
        except Exception as e:
            log.warning("Morning briefing send failed for user %d: %s", user["id"], e)

    return {"sent": sent, "total_users": len(users)}


# Cron: every Monday at 08:00 UTC.
register_cron("send_weekly_digest_batch", weekday=0, hour=8, minute=0)
# Morning briefing: daily at 08:03 UTC.
register_cron("send_morning_briefings", hour=8, minute=3)
