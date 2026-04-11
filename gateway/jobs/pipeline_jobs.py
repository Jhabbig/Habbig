"""Pipeline, deletion and sitemap jobs."""

from __future__ import annotations

import logging
import time
from typing import Any

from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.pipeline")


@register_job("run_pipeline")
async def run_pipeline() -> dict[str, Any]:
    """Kick off a scraper run via the scraper service.

    This is the enqueue-from-admin entry point. Routes that previously
    called `db.subscribe_newsletter`-style inline logic should delegate
    here when they need a full pipeline refresh.
    """
    import httpx
    import os

    scraper_url = os.environ.get("SCRAPER_URL", "").strip()
    if not scraper_url:
        return {"ok": False, "error": "SCRAPER_URL not set"}
    api_key = os.environ.get("SCRAPER_API_KEY", "")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{scraper_url.rstrip('/')}/pull",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    return {"ok": resp.status_code == 200, "status": resp.status_code}


@register_job("process_scheduled_deletions")
async def process_scheduled_deletions() -> dict[str, Any]:
    """Hard-delete users whose 30-day soft-delete window has elapsed.

    Anonymises personal fields, cascades personal data, retains research
    and financial records. Sends a final `account_deleted` email (which
    will bounce — intentional: confirms deletion to any proxied inbox).
    """
    import db
    from jobs.email_jobs import enqueue_email

    now = int(time.time())
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, email, username FROM users "
            "WHERE deletion_scheduled_for IS NOT NULL "
            "AND deletion_scheduled_for <= ? "
            "AND deletion_cancelled_at IS NULL "
            "AND COALESCE(is_deleted, 0) = 0",
            (now,),
        ).fetchall()

    deleted = 0
    for r in rows:
        user_id = r["id"]
        old_email = r["email"]
        anon_email = f"deleted_{user_id}@deleted.narve.ai"
        try:
            with db.conn() as c:
                # Anonymise
                c.execute(
                    "UPDATE users SET email = ?, username = ?, "
                    "is_deleted = 1, deleted_at = ?, "
                    "password_hash = '', password_salt = '', "
                    "onboarding_categories = NULL, "
                    "notify_push = 0, notify_email = 0, "
                    "email_digest = 0, email_marketing = 0 "
                    "WHERE id = ?",
                    (anon_email, f"[deleted_{user_id}]", now, user_id),
                )
                # Cascade personal data
                c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                c.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
                c.execute("DELETE FROM email_unsubscribes WHERE user_id = ?", (user_id,))
                c.execute("DELETE FROM user_topics WHERE user_id = ?", (user_id,))
                c.execute("DELETE FROM intelligence_conversations WHERE user_id = ?", (user_id,))
                c.execute("DELETE FROM gifted_subscriptions WHERE user_id = ?", (user_id,))
                c.execute("DELETE FROM user_market_credentials WHERE user_id = ?", (user_id,))
                c.execute("DELETE FROM user_market_views WHERE user_id = ?", (user_id,))
                c.execute("DELETE FROM feedback_submissions WHERE user_id = ?", (user_id,))
                # NOTE: subscriptions, analytics_events, user_bet_history retained
                # (financial/research records — retained for legal compliance).
            # Final courtesy email — will likely bounce, that is the point.
            try:
                await enqueue_email(
                    to=old_email,
                    template="account_deleted",
                    context={"app_url": "https://narve.ai"},
                    tags=["account_deleted"],
                )
            except Exception:
                pass
            deleted += 1
        except Exception as e:
            log.exception("hard-delete failed for user_id=%s: %s", user_id, e)

    return {"deleted": deleted, "checked": len(rows)}


@register_job("generate_sitemap")
async def generate_sitemap() -> dict[str, Any]:
    """Regenerate the sitemap.xml file on disk.

    Runs daily via cron, and on-demand after the pipeline resolves new
    sources. Writes to `static/sitemap.xml` so the static mount serves it.
    """
    import db
    from pathlib import Path

    base_url = "https://narve.ai"
    parts: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>']
    parts.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')

    static_urls = [
        ("/", "1.0", "daily"),
        ("/terms", "0.5", "monthly"),
        ("/privacy", "0.5", "monthly"),
        ("/pricing", "0.8", "weekly"),
    ]
    for url, priority, changefreq in static_urls:
        parts.append(
            f"<url><loc>{base_url}{url}</loc>"
            f"<priority>{priority}</priority>"
            f"<changefreq>{changefreq}</changefreq></url>"
        )

    # Public source profiles — only include rated sources.
    try:
        sources = db.list_all_source_credibilities() if hasattr(db, "list_all_source_credibilities") else []
        for s in sources:
            if not s["accuracy_unlocked"]:
                continue
            handle = s["source_handle"]
            parts.append(
                f"<url><loc>{base_url}/sources/{handle}</loc>"
                f"<priority>0.7</priority>"
                f"<changefreq>weekly</changefreq></url>"
            )
    except Exception as e:
        log.warning("sitemap source listing failed: %s", e)

    parts.append("</urlset>")
    xml = "\n".join(parts)

    sitemap_path = Path(__file__).parent.parent / "static" / "sitemap.xml"
    sitemap_path.write_text(xml)
    return {"urls": len(parts) - 2, "path": str(sitemap_path)}


@register_job("run_backtest")
async def run_backtest_job(backtest_id: int = 0) -> dict[str, Any]:
    """Execute a backtest simulation (F13)."""
    import db as _db
    import json as _json
    from intelligence.backtester import run_backtest

    now = int(time.time())
    with _db.conn() as c:
        row = c.execute("SELECT * FROM backtests WHERE id = ?", (backtest_id,)).fetchone()
    if not row:
        return {"error": "backtest not found"}

    with _db.conn() as c:
        c.execute("UPDATE backtests SET status = 'running' WHERE id = ?", (backtest_id,))

    try:
        params = _json.loads(row["params"])
        result = run_backtest(params)
        with _db.conn() as c:
            c.execute(
                "UPDATE backtests SET status = 'completed', result = ?, completed_at = ? WHERE id = ?",
                (_json.dumps(result), now, backtest_id),
            )
        return {"backtest_id": backtest_id, "status": "completed", "trade_count": result.get("trade_count", 0)}
    except Exception as e:
        log.exception("Backtest %d failed: %s", backtest_id, e)
        with _db.conn() as c:
            c.execute(
                "UPDATE backtests SET status = 'failed', result = ?, completed_at = ? WHERE id = ?",
                (_json.dumps({"error": str(e)}), now, backtest_id),
            )
        return {"backtest_id": backtest_id, "status": "failed", "error": str(e)}


@register_job("recompute_credibilities")
async def recompute_credibilities_job() -> dict[str, Any]:
    """Recompute all source credibility scores using Bayesian time-decay.

    Runs on a 6-hour cadence so scores stay fresh as predictions resolve.
    Also callable on-demand via admin panel or after a resolution batch.
    """
    import db as _db
    start = time.monotonic()
    count = _db.recompute_all_credibilities()
    duration = round(time.monotonic() - start, 2)
    log.info("Credibility recompute finished: %d sources in %.2fs", count, duration)
    return {"recomputed": count, "duration_seconds": duration}


@register_job("poll_whale_positions")
async def poll_whale_positions_job() -> dict[str, Any]:
    """Poll whale wallet positions on Polymarket (F14)."""
    from backend.markets.whale_tracker import poll_whale_positions
    return await poll_whale_positions()


# Cron schedules
register_cron("process_scheduled_deletions", hour=2, minute=0)   # daily 02:00 UTC
register_cron("generate_sitemap", hour=6, minute=0)              # daily 06:00 UTC
# Whale position polling: hourly at :47
register_cron("poll_whale_positions", minute=47)
# Credibility recompute every 6 hours — 4 entries, same job name.
register_cron("recompute_credibilities", hour=0, minute=15)      # 00:15 UTC
register_cron("recompute_credibilities", hour=6, minute=15)      # 06:15 UTC
register_cron("recompute_credibilities", hour=12, minute=15)     # 12:15 UTC
register_cron("recompute_credibilities", hour=18, minute=15)     # 18:15 UTC
