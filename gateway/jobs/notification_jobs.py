"""Market resolution and push notification jobs."""

from __future__ import annotations

import logging
from typing import Any

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.notifications")


@register_job("send_market_resolution_notifications")
async def send_market_resolution_notifications(
    market_slug: str,
    outcome: str,
    market_question: str | None = None,
    batch_size: int = 100,
) -> dict[str, Any]:
    """Notify every user who viewed a market that has just resolved.

    Batched: processes up to `batch_size` users per run and re-enqueues
    itself if more remain. This keeps any single job run bounded.
    """
    import db
    from jobs.email_jobs import enqueue_email
    from jobs import enqueue_job

    # Collect unnotified viewers.
    with db.conn() as c:
        rows = c.execute(
            "SELECT umv.id, umv.user_id, u.email, u.username, u.email_marketing "
            "FROM user_market_views umv "
            "INNER JOIN users u ON umv.user_id = u.id "
            "WHERE umv.market_slug = ? AND umv.notified_on_resolution = 0 "
            "AND COALESCE(u.is_deleted, 0) = 0 "
            "LIMIT ?",
            (market_slug, batch_size),
        ).fetchall()

    if not rows:
        return {"notified": 0, "more": False}

    # Gather prediction summary for the context.
    preds = db.get_predictions_for_market(market_slug) if hasattr(db, "get_predictions_for_market") else []
    correct_count = sum(1 for p in preds if p["resolved"] and p["resolved_correct"])
    total_count = len(preds)

    notified = 0
    for r in rows:
        # Don't skip based on marketing opt-out — resolution notifications
        # are transactional-adjacent (the user viewed the market themselves).
        if not r["email"]:
            continue
        try:
            await enqueue_email(
                to=r["email"],
                template="market_resolved",
                context={
                    "display_name": r["username"] or (r["email"] or "").split("@")[0],
                    "market_question": market_question or market_slug,
                    "outcome": outcome,
                    "correct_count": correct_count,
                    "total_count": total_count,
                    "market_url": f"https://polymarket.com/event/{market_slug}",
                    "app_url": "https://narve.ai",
                },
                tags=["market_resolved"],
            )
            with db.conn() as c:
                c.execute(
                    "UPDATE user_market_views SET notified_on_resolution = 1 WHERE id = ?",
                    (r["id"],),
                )
            notified += 1
            # Parallel push fanout (opt-in, best-effort). The email is
            # the authoritative delivery; push is a convenience nudge.
            await enqueue_job(
                "send_push_notification",
                user_id=r["user_id"],
                title="Market resolved",
                body=(market_question or market_slug)[:140] + " \u2192 " + outcome,
                url=f"/markets/{market_slug}",
                tag=f"market-resolved-{market_slug}",
            )
        except Exception as e:
            log.warning("resolution notif failed for user_id=%s: %s", r["user_id"], e)

    # Re-enqueue if there might be more.
    more = len(rows) == batch_size
    if more:
        await enqueue_job(
            "send_market_resolution_notifications",
            market_slug=market_slug,
            outcome=outcome,
            market_question=market_question,
            batch_size=batch_size,
        )
    return {"notified": notified, "more": more}


@register_job("send_push_notification")
async def send_push_notification(
    user_id: int,
    title: str,
    body: str,
    url: str | None = None,
    tag: str | None = None,
    data: dict | None = None,
) -> dict[str, Any]:
    """Dispatch a Web Push to every subscription the user owns.

    Delegates to push.send_to_user() which walks push_subscriptions rows,
    calls pywebpush, and cleans up rows the push service reports as gone.
    Silently no-ops when pywebpush / VAPID keys are unavailable — the
    caller still has email as a parallel channel.
    """
    try:
        import push  # lazy import: module is safe even when pywebpush missing
        result = push.send_to_user(
            user_id=user_id,
            title=title,
            body=body,
            url=url or "/",
            tag=tag or "narve-general",
            data=data or {},
        )
        return result
    except push.PushNotAvailable as exc:  # type: ignore[attr-defined]
        # Not configured (no keys, pywebpush missing). Normal in dev.
        log.debug("push not available for user_id=%s: %s", user_id, exc)
        return {"sent": False, "reason": str(exc)}
    except Exception as exc:  # pragma: no cover — unexpected
        log.warning("push fanout failed user_id=%s: %s", user_id, exc)
        return {"sent": False, "reason": "error"}


def _fanout_push_safe(user_id: int, *, title: str, body: str, url: str, tag: str) -> None:
    """Fire-and-forget push fanout used by the email jobs above.

    Each email-fanout job enqueues a push job alongside the email send.
    We swallow every error here — push is a best-effort parallel channel,
    never a reason for an email-fanout job to fail its DB commit.
    """
    try:
        from jobs import enqueue_job
        import asyncio
        # enqueue_job is async; we're inside another async job already so
        # use ensure_future / awaitable at call sites. This wrapper just
        # exposes a sync-looking helper; callers that can await should
        # prefer `await enqueue_job(...)` directly. Kept sync-friendly
        # here for readability at the call sites.
        coro = enqueue_job(
            "send_push_notification",
            user_id=user_id,
            title=title,
            body=body,
            url=url,
            tag=tag,
        )
        task = asyncio.ensure_future(coro)
        # Swallow the future's result silently; we're best-effort.
        task.add_done_callback(lambda _t: None)
    except Exception:
        pass


@register_job("send_saved_prediction_resolution_notifications")
async def send_saved_prediction_resolution_notifications(
    batch_size: int = 200,
) -> dict[str, Any]:
    """Notify every user whose saved prediction just resolved (Feature 12).

    Runs every 10 minutes via cron. Scans ``saved_predictions`` joined with
    ``predictions`` for rows where the prediction has ``resolved = 1`` AND
    the saved row still has ``notified_on_resolution = 0``. Dispatches an
    email per row, then marks the saved row notified. Idempotent: the
    ``notified_on_resolution`` flag ensures we never double-send, and
    batching caps each run.
    """
    import db
    from jobs.email_jobs import enqueue_email
    from jobs import enqueue_job

    # Find unnotified resolved saves, newest first, across every user.
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT sp.id AS saved_id, sp.user_id, sp.notes,
                   p.id AS prediction_id, p.content, p.source_handle,
                   p.resolved_correct, p.market_id,
                   u.email, u.username
            FROM saved_predictions sp
            JOIN predictions p ON p.id = sp.prediction_id
            JOIN users u ON u.id = sp.user_id
            WHERE sp.notified_on_resolution = 0
              AND p.resolved = 1
              AND u.email IS NOT NULL
              AND COALESCE(u.is_deleted, 0) = 0
            ORDER BY p.resolved_at DESC
            LIMIT ?
            """,
            (batch_size,),
        ).fetchall()

    if not rows:
        return {"notified": 0, "more": False}

    notified = 0
    skipped = 0
    for r in rows:
        correct = bool(r["resolved_correct"])
        outcome = "correct" if correct else "incorrect"
        display_name = r["username"] or (r["email"] or "").split("@")[0]
        try:
            await enqueue_email(
                to=r["email"],
                template="saved_prediction_resolved",
                context={
                    "display_name": display_name,
                    "prediction_text": r["content"],
                    "source_handle": r["source_handle"],
                    "outcome": outcome,
                    "correct": correct,
                    "user_note": r["notes"] or "",
                    "saved_url": "https://narve.ai/saved",
                },
                tags=["saved_prediction_resolved"],
            )
            db.mark_saved_prediction_notified(r["saved_id"])
            notified += 1
            # Push parallel to email — same transactional context
            # (the user themselves saved this prediction, so they're
            # guaranteed to want to know).
            await enqueue_job(
                "send_push_notification",
                user_id=r["user_id"],
                title="Saved prediction " + ("resolved correct" if correct else "resolved incorrect"),
                body=(r["content"] or "")[:140],
                url="/saved",
                tag=f"saved-{r['saved_id']}",
            )
        except Exception as exc:
            log.warning("saved-prediction resolution notif failed for saved_id=%s user_id=%s: %s",
                        r["saved_id"], r["user_id"], exc)
            skipped += 1

    more = len(rows) == batch_size
    if more:
        # Chain the next batch so one cron tick drains the queue when catching up.
        await enqueue_job(
            "send_saved_prediction_resolution_notifications",
            batch_size=batch_size,
        )
    return {"notified": notified, "skipped": skipped, "more": more}


@register_job("check_market_movers")
async def check_market_movers(
    price_change_threshold: float = 0.08,
    lookback_hours: int = 2,
) -> dict[str, Any]:
    """Detect significant market moves backed by credibility intelligence (F8).

    Compares current prices to snapshots from lookback_hours ago. When a market
    moves more than price_change_threshold AND narve.ai has high-credibility
    source intelligence, sends alerts to opted-in users.
    """
    import db
    import os
    import time as _time

    app_url = os.environ.get("APP_URL", "https://narve.ai")
    now = int(_time.time())
    lookback_ts = now - lookback_hours * 3600

    # Fetch and enrich markets
    try:
        from backend.markets import unified_markets
        from backend.markets.polymarket_client import PolymarketClient
        from backend.markets.kalshi_client import KalshiClient

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
        log.exception("Market mover check: fetch failed: %s", e)
        return {"alerts_sent": 0, "error": str(e)}

    # Get users who want alerts
    with db.conn() as c:
        alert_users = c.execute(
            "SELECT id, email, username, notify_ev_threshold, notify_cred_threshold "
            "FROM users WHERE notify_email = 1 AND COALESCE(is_deleted, 0) = 0 "
            "AND COALESCE(email_unsubscribed_at, 0) = 0"
        ).fetchall()

    if not alert_users:
        return {"alerts_sent": 0, "movers_found": 0, "reason": "no opted-in users"}

    alerts_sent = 0
    movers = []

    for market in enriched:
        # Get old snapshot
        slug = market.id.split(":", 1)[1] if ":" in market.id else market.id
        old_snap = db.get_market_snapshot_at(slug, lookback_ts)
        if not old_snap:
            continue

        price_change = market.yes_price - old_snap["yes_price"]
        if abs(price_change) < price_change_threshold:
            continue
        if market.betyc_prediction_count < 1:
            continue

        # Find the top credibility source prediction
        preds = db.get_predictions_for_market(market.id)
        top_source = None
        for p in preds:
            cred = p.get("global_credibility") or 0
            if cred >= 0.5:
                days_ago = max(0, (now - (p["extracted_at"] or now)) // 86400)
                top_source = {
                    "handle": p["source_handle"],
                    "credibility": round(cred, 2),
                    "direction": p["direction"] or "?",
                    "days_ago": days_ago,
                }
                break

        movers.append({
            "market": market,
            "price_change": price_change,
            "old_price": old_snap["yes_price"],
            "top_source": top_source,
        })

    # Send alerts to each user for each mover that passes their thresholds
    for mover in movers:
        m = mover["market"]
        for user in alert_users:
            # Check user thresholds
            ev_thresh = user["notify_ev_threshold"] or 0
            cred_thresh = user["notify_cred_threshold"] or 0
            if abs(m.betyc_ev_score or 0) < ev_thresh:
                continue
            if (m.betyc_avg_credibility or 0) < cred_thresh:
                continue

            context = {
                "app_url": app_url,
                "market_title": m.title[:100],
                "price_change": mover["price_change"],
                "price_change_display": f"{'+' if mover['price_change'] > 0 else ''}{int(mover['price_change'] * 100)}pp",
                "current_price": int(m.yes_price * 100),
                "previous_price": int(mover["old_price"] * 100),
                "lookback_hours": lookback_hours,
                "top_source": mover["top_source"],
                "unsubscribe_url": f"{app_url}/unsubscribe?type=digest",
            }

            try:
                await enqueue_email(
                    to=user["email"],
                    template="market_mover_alert",
                    context=context,
                    tags=["market_mover_alert"],
                )
                alerts_sent += 1
                # Push fanout for the high-credibility mover case.
                # Only fire when there's a top_source backing the move —
                # movement alone is noise; movement + a high-cred take
                # is the "insider_signal_high_confidence" event type
                # the PWA spec calls out.
                if mover.get("top_source"):
                    from jobs import enqueue_job
                    await enqueue_job(
                        "send_push_notification",
                        user_id=user["id"],
                        title="High-cred market move",
                        body=f"{m.title[:80]} moved {context['price_change_display']}",
                        url=f"/markets/{m.id}",
                        tag=f"mover-{m.id}",
                    )
            except Exception as e:
                log.warning("Market mover alert failed for user %d: %s", user["id"], e)

    return {"alerts_sent": alerts_sent, "movers_found": len(movers)}


# Run hourly at minute=7. The registry only supports integer minute values
# so we can't do every-10-min; a 1-hour lag on resolution notifications is
# acceptable — users typically aren't watching in real time and this keeps
# the batch size bounded. Minute=7 avoids the herd on :00.
register_cron(
    "send_saved_prediction_resolution_notifications",
    minute=7,
)
# Market mover alerts: hourly at :32 (offset from other cron jobs).
register_cron("check_market_movers", minute=32)
