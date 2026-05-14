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

    # ── N+1 fix ──────────────────────────────────────────────────────────
    # _resolve_subproduct_filter() costs 3 queries per user (admin probe
    # + active-subs for tier + active-subs for slugs). For a digest run
    # we batch-prefetch all of that up front, and hoist the two
    # user-invariant catalog queries (recent predictions + source
    # credibilities) plus the per-user "categories → sources" DISTINCT
    # scan out of the loop.
    from subproduct_filters import categories_for as _cats_for
    _uids = [u["id"] for u in users]
    _admin_ids: set[int] = set()
    _user_plans: dict[int, list[str]] = {}
    _subproducts_by_uid: dict[int, set[str]] = {}
    if _uids:
        with db.conn() as c:
            for i in range(0, len(_uids), 500):  # SQLITE_MAX_VARIABLE_NUMBER=999
                ch = _uids[i:i + 500]
                ph = ",".join("?" * len(ch))
                for r in c.execute(
                    f"SELECT id FROM users WHERE id IN ({ph}) AND is_admin = 1", ch
                ).fetchall():
                    _admin_ids.add(r["id"])
                for r in c.execute(
                    f"SELECT user_id, dashboard_key, plan FROM subscriptions "
                    f"WHERE user_id IN ({ph}) AND status = 'active' "
                    f"AND (expires_at IS NULL OR expires_at > ?)",
                    (*ch, now),
                ).fetchall():
                    _user_plans.setdefault(r["user_id"], []).append(r["plan"] or "")
                    if r["dashboard_key"] != "__plan__":
                        _subproducts_by_uid.setdefault(r["user_id"], set()).add(r["dashboard_key"])

    def _resolve_batched(uid: int):
        if uid in _admin_ids:
            return None, [], True
        plans = _user_plans.get(uid, [])
        if not plans:
            return set(), [], False
        if any((p or "").startswith("pro") for p in plans):
            return None, [], True
        slugs = _subproducts_by_uid.get(uid, set())
        if not slugs:
            return set(), [], False
        cats: set[str] = set()
        for s in slugs:
            cats.update(_cats_for(s))
        labels = sorted(_subproduct_display_name(s) for s in slugs)
        return (cats or None), labels, True

    _prediction_pool: list[dict] = []
    try:
        for p in db.list_recent_predictions(limit=200):
            cred = p["global_credibility"] if "global_credibility" in p.keys() else None
            _prediction_pool.append({
                "source": f"@{p['source_handle']}",
                "content": (p["content"] or "")[:200],
                "credibility": round(cred, 2) if cred is not None else None,
                "category": p["category"],
            })
    except Exception:
        pass

    _all_sources: list = []
    try:
        _all_sources = (
            list(db.list_all_source_credibilities())
            if hasattr(db, "list_all_source_credibilities") else []
        )
    except Exception:
        _all_sources = []

    _source_cats: dict[str, set[str]] = {}
    try:
        with db.conn() as c:
            for r in c.execute(
                "SELECT DISTINCT source_handle, category FROM predictions "
                "WHERE extracted_at >= ?",
                (week_start,),
            ).fetchall():
                _source_cats.setdefault(r["source_handle"], set()).add(r["category"])
    except Exception:
        pass
    # ──────────────────────────────────────────────────────────────────────

    enqueued = 0
    skipped = 0
    for u in users:
        category_filter, subproduct_labels, should_send = _resolve_batched(u["id"])
        if not should_send:
            skipped += 1
            continue

        top_predictions: list[dict] = []
        for p in _prediction_pool:
            if category_filter is not None and p["category"] not in category_filter:
                continue
            top_predictions.append(p)
            if len(top_predictions) >= 5:
                break

        if category_filter is not None:
            allowed_handles = {
                h for h, cats in _source_cats.items() if cats & category_filter
            }
            _scoped = [s for s in _all_sources if s["source_handle"] in allowed_handles]
        else:
            _scoped = list(_all_sources)
        _scoped = sorted(
            [s for s in _scoped if s["total_predictions"] >= 5],
            key=lambda s: s["global_credibility"],
            reverse=True,
        )[:3]
        top_sources: list[dict] = [
            {
                "handle": s["source_handle"],
                "credibility": round(s["global_credibility"], 2),
                "accuracy": (
                    f"{int(100 * s['correct_predictions'] / max(s['total_predictions'], 1))}%"
                ),
            }
            for s in _scoped
        ]

        context = {
            "display_name": u["username"] or (u["email"] or "").split("@")[0],
            "week_start": _dt.datetime.fromtimestamp(week_start).strftime("%b %d"),
            "week_end": _dt.datetime.fromtimestamp(now).strftime("%b %d, %Y"),
            "top_predictions": top_predictions,
            "top_sources": top_sources,
            "subproduct_labels": subproduct_labels,
            "subproduct_labels_str": ", ".join(subproduct_labels),
            "unsubscribe_url": _unsub_url(u["id"], u["email"], "digest"),
        }
        # Per-recipient watermark — forensic attribution for Pro intelligence
        # email leaks. See email_system/watermark.py.
        from email_system import watermark as _wm
        _wm.annotate_context(context, u["id"], "weekly_digest", batch_ts=now)
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

    # ── N+1 fix ──────────────────────────────────────────────────────────
    # Batch all per-user lookups up front:
    #   - tier (was 2 queries each via get_user_subscription_tier)
    #   - active subproducts (was 1 query each)
    #   - followed-source signals (was 1 query each, with category narrow)
    # That's ~4 per-user queries collapsed to a handful of chunked
    # batched queries up front.
    _uids_mb = [u["id"] for u in users]
    _admin_mb: set[int] = set()
    _plans_mb: dict[int, list[str]] = {}
    _subp_mb: dict[int, set[str]] = {}
    if _uids_mb:
        with db.conn() as c:
            for i in range(0, len(_uids_mb), 500):
                ch = _uids_mb[i:i + 500]
                ph = ",".join("?" * len(ch))
                for r in c.execute(
                    f"SELECT id FROM users WHERE id IN ({ph}) AND is_admin = 1", ch
                ).fetchall():
                    _admin_mb.add(r["id"])
                for r in c.execute(
                    f"SELECT user_id, dashboard_key, plan FROM subscriptions "
                    f"WHERE user_id IN ({ph}) AND status = 'active' "
                    f"AND (expires_at IS NULL OR expires_at > ?)",
                    (*ch, now),
                ).fetchall():
                    _plans_mb.setdefault(r["user_id"], []).append(r["plan"] or "")
                    if r["dashboard_key"] != "__plan__":
                        _subp_mb.setdefault(r["user_id"], set()).add(r["dashboard_key"])

    def _tier_mb(uid: int) -> str:
        if uid in _admin_mb:
            return "pro"
        plans = _plans_mb.get(uid, [])
        if any((p or "").startswith("pro") for p in plans):
            return "pro"
        if plans:
            return "trader"
        return "none"

    # Pre-fetch all followed-source signals for the cohort. We pull
    # category too so per-user category narrowing happens in-memory.
    _signals_mb: dict[int, list[dict]] = {uid: [] for uid in _uids_mb}
    if _uids_mb:
        with db.conn() as c:
            for i in range(0, len(_uids_mb), 500):
                ch = _uids_mb[i:i + 500]
                ph = ",".join("?" * len(ch))
                rows = c.execute(
                    f"SELECT fs.user_id, p.source_handle, p.content, p.category, "
                    f"sc.global_credibility "
                    f"FROM predictions p "
                    f"JOIN followed_sources fs ON fs.source_handle = p.source_handle "
                    f"LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
                    f"WHERE fs.user_id IN ({ph}) AND p.extracted_at >= ? "
                    f"ORDER BY p.extracted_at DESC",
                    (*ch, yesterday),
                ).fetchall()
                for r in rows:
                    _signals_mb[r["user_id"]].append({
                        "source_handle": r["source_handle"],
                        "content": (r["content"] or "")[:120],
                        "category": r["category"],
                        "credibility": round(r["global_credibility"] or 0.5, 2),
                    })
    # ──────────────────────────────────────────────────────────────────────

    sent = 0
    skipped = 0
    for user in users:
        tier = _tier_mb(user["id"])
        if tier == "none":
            skipped += 1
            continue
        is_pro = tier == "pro"
        slugs = _subp_mb.get(user["id"], set())
        if not is_pro and not slugs:
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

        # New signals from the user's pre-fetched bucket; apply the
        # category narrow in-memory (preserves the original LIMIT 5).
        _bucket = _signals_mb.get(user["id"], [])
        new_signals: list[dict] = []
        for s in _bucket:
            if (not is_pro) and category_filter and s["category"] not in category_filter:
                continue
            new_signals.append({
                "source_handle": s["source_handle"],
                "content": s["content"],
                "credibility": s["credibility"],
            })
            if len(new_signals) >= 5:
                break

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
            "subproduct_labels_str": ", ".join(subproduct_labels),
            "unsubscribe_url": f"{app_url}/unsubscribe?type=digest",
        }
        # Per-recipient watermark — forensic attribution for Pro intelligence
        # email leaks. See email_system/watermark.py.
        from email_system import watermark as _wm
        _wm.annotate_context(context, user["id"], "morning_briefing", batch_ts=now)

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
