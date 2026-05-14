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


def _subproduct_display_name(slug: str) -> str:
    """Resolve a dashboard_key to a friendly label for the digest header.

    Falls back to a title-cased slug if config can't be imported (tests
    that don't boot the gateway). Keeps the import lazy so this module
    stays cheap to import.
    """
    try:
        from server import DASHBOARDS
        cfg = DASHBOARDS.get(slug) or {}
        name = cfg.get("display_name")
        if name:
            return name
    except Exception:
        pass
    return slug.replace("_", " ").title()


def _resolve_subproduct_filter(user_id: int) -> tuple[set[str] | None, list[str], bool]:
    """Decide which subproducts a user's digest should cover.

    Returns ``(category_filter, label_list, should_send)``:

      * ``category_filter`` — set of prediction.category values to keep,
        or ``None`` for "no filter, show everything" (Pro tier and admins).
      * ``label_list`` — friendly labels for the "Your digest for: …"
        header; empty when no filter applied.
      * ``should_send`` — False when the user has no active subscription;
        callers must skip the send.
    """
    import db
    from subproduct_filters import categories_for

    tier = db.get_user_subscription_tier(user_id) if hasattr(db, "get_user_subscription_tier") else "none"
    if tier == "none":
        return set(), [], False
    if tier == "pro":
        return None, [], True  # Pro: show everything across all 12 subproducts.

    slugs = db.get_user_active_subproducts(user_id) if hasattr(db, "get_user_active_subproducts") else set()
    if not slugs:
        return set(), [], False

    # Union the category whitelists across every slug the user owns.
    cats: set[str] = set()
    for slug in slugs:
        cats.update(categories_for(slug))
    labels = sorted(_subproduct_display_name(s) for s in slugs)
    # Empty cats (e.g. only 'traders' which is a platform filter, not a
    # category filter) means "no category narrowing" — fall back to None
    # so the user still gets content. The label still scopes the header.
    return (cats or None), labels, True


@register_job("send_weekly_digest_batch")
async def send_weekly_digest_batch() -> dict[str, Any]:
    """Send the weekly digest to every opted-in active subscriber.

    Processes in batches of 50 users to keep memory flat. Each user's
    digest is enqueued as an individual `send_email` job so a single
    rendering failure doesn't kill the whole run.

    Content is filtered to each user's active subproducts: a Crypto-only
    subscriber sees only crypto predictions/sources, not the other 11
    subproducts they don't pay for. Pro users get every subproduct.
    Users with no active subscription are skipped entirely so the
    digest never reaches expired accounts.
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
        category_filter, subproduct_labels, should_send = _resolve_subproduct_filter(u["id"])
        if not should_send:
            # tier='none' OR no active subproducts → skip the send entirely.
            # Avoids spamming expired/cancelled users with content from
            # the 11 subproducts they're not paying for.
            skipped += 1
            continue

        # Top 5 high-EV predictions from the last week, restricted to
        # the user's subproducts when a category filter is set.
        top_predictions: list[dict] = []
        try:
            # Pull a wider window then filter in-memory so we still
            # have 5 picks after the category narrow.
            pull_limit = 5 if category_filter is None else 50
            preds = db.list_recent_predictions(limit=pull_limit)
            for p in preds:
                if category_filter is not None and p["category"] not in category_filter:
                    continue
                cred = p["global_credibility"] if "global_credibility" in p.keys() else None
                top_predictions.append({
                    "source": f"@{p['source_handle']}",
                    "content": (p["content"] or "")[:200],
                    "credibility": round(cred, 2) if cred is not None else None,
                    "category": p["category"],
                })
                if len(top_predictions) >= 5:
                    break
        except Exception:
            pass

        # Top 3 most accurate sources, optionally restricted to sources
        # that have been active in the user's categories this week.
        top_sources: list[dict] = []
        try:
            sources = db.list_all_source_credibilities() if hasattr(db, "list_all_source_credibilities") else []
            if category_filter is not None:
                # Keep only sources that have at least one prediction in
                # one of the user's categories within the digest window.
                placeholders = ",".join("?" * len(category_filter))
                with db.conn() as c:
                    allowed = c.execute(
                        f"SELECT DISTINCT source_handle FROM predictions "
                        f"WHERE category IN ({placeholders}) AND extracted_at >= ?",
                        (*sorted(category_filter), week_start),
                    ).fetchall()
                allowed_handles = {r["source_handle"] for r in allowed}
                sources = [s for s in sources if s["source_handle"] in allowed_handles]
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
            "subproduct_labels": subproduct_labels,
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
      - Top 5 markets by |betyc_edge| (restricted to user's subproducts)
      - New predictions from followed sources (last 24h, same restriction)
      - Markets approaching resolution (same restriction)

    Users with no active subscription are skipped — the briefing never
    reaches expired accounts. Pro users see all 12 subproducts; single-
    sub users see only the subproduct(s) they pay for.
    """
    import db
    import os
    import time as _time
    from backend.markets import unified_markets
    from backend.markets.polymarket_client import PolymarketClient
    from backend.markets.kalshi_client import KalshiClient
    from subproduct_filters import filter_by_subproduct, categories_for

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

    # Pre-compute approaching resolutions for the full pool once; we
    # partition per-user later by filtering the same list.
    from datetime import datetime, timezone
    approaching_all = []
    for m in enriched:
        if m.close_time:
            try:
                close_dt = datetime.fromisoformat(m.close_time.replace("Z", "+00:00"))
                days_until = (close_dt - datetime.now(timezone.utc)).days
                if 0 <= days_until <= 7:
                    approaching_all.append((m, close_dt))
            except (ValueError, TypeError):
                pass

    sent = 0
    skipped = 0
    for user in users:
        # Resolve which subproducts this user should see content from.
        tier = (
            db.get_user_subscription_tier(user["id"])
            if hasattr(db, "get_user_subscription_tier")
            else "none"
        )
        if tier == "none":
            skipped += 1
            continue
        is_pro = tier == "pro"
        slugs = (
            db.get_user_active_subproducts(user["id"])
            if hasattr(db, "get_user_active_subproducts")
            else set()
        )
        if not is_pro and not slugs:
            # No active subproducts → skip. Avoids spamming expired
            # users with the 11 subproducts they're not paying for.
            skipped += 1
            continue

        # Build per-user market scope. Pro = full pool; everyone else =
        # union of their subproducts' filters (deduped by market id).
        if is_pro:
            user_markets = enriched
            category_filter: set[str] | None = None
        else:
            seen_ids: set = set()
            user_markets = []
            for slug in slugs:
                for m in filter_by_subproduct(enriched, slug):
                    if m.id in seen_ids:
                        continue
                    seen_ids.add(m.id)
                    user_markets.append(m)
            category_filter = set()
            for slug in slugs:
                category_filter.update(categories_for(slug))

        # Top 5 by absolute edge within the user's market scope
        with_edge = [m for m in user_markets if m.betyc_ev_score is not None and m.betyc_prediction_count >= 1]
        with_edge.sort(key=lambda m: abs(m.betyc_ev_score or 0), reverse=True)
        top_5 = with_edge[:5]

        # Approaching resolutions, scoped to user_markets
        if is_pro:
            scoped_approaching = approaching_all
        else:
            user_market_ids = {m.id for m in user_markets}
            scoped_approaching = [(m, dt) for m, dt in approaching_all if m.id in user_market_ids]
        approaching = [
            {"title": m.title, "close_time": dt.strftime("%b %d")}
            for m, dt in scoped_approaching[:5]
        ]

        # New signals from followed sources (last 24h), gated by category
        if is_pro or not category_filter:
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
        else:
            placeholders = ",".join("?" * len(category_filter))
            with db.conn() as c:
                new_signals_rows = c.execute(
                    f"SELECT p.source_handle, p.content, sc.global_credibility "
                    f"FROM predictions p "
                    f"JOIN followed_sources fs ON fs.source_handle = p.source_handle AND fs.user_id = ? "
                    f"LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
                    f"WHERE p.extracted_at >= ? "
                    f"AND p.category IN ({placeholders}) "
                    f"ORDER BY p.extracted_at DESC LIMIT 5",
                    (user["id"], yesterday, *sorted(category_filter)),
                ).fetchall()

        new_signals = [
            {
                "source_handle": r["source_handle"],
                "content": (r["content"] or "")[:120],
                "credibility": round(r["global_credibility"] or 0.5, 2),
            }
            for r in new_signals_rows
        ]

        subproduct_labels = (
            [] if is_pro else sorted(_subproduct_display_name(s) for s in slugs)
        )

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
            "subproduct_labels": subproduct_labels,
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

    return {"sent": sent, "skipped": skipped, "total_users": len(users)}


# Cron: every Monday at 08:00 UTC.
register_cron("send_weekly_digest_batch", weekday=0, hour=8, minute=0)
# Morning briefing: daily at 08:03 UTC.
register_cron("send_morning_briefings", hour=8, minute=3)
