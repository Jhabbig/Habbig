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

    GDPR Art. 17 hard-delete pass. The previous implementation hand-rolled
    a 7-table DELETE that missed ~50 user-keyed tables (intelligence
    messages, watchlists, alerts, follows, take_reports, newsletter
    audiences, etc.) and every ``*_user_id`` variant column
    (referrer_user_id, admin_user_id, sharer_user_id, ...). We now route
    through ``db.cascade_delete_user`` which walks ``sqlite_master`` and
    deletes every row in every (table, column) pair matching ``user_id``
    or ``*_user_id`` — same engine the user-initiated self-delete uses,
    so the two paths stay in lock-step.

    We anonymise the ``users`` row's PII first (defence in depth — if
    the cascade aborts partway, the PII is already wiped), then cascade
    every other user-keyed row across the schema. The cascade handles
    the final users DELETE itself.

    We also unlink any data export ZIPs on disk so the deletion is
    GDPR-clean and reclaims disk. The courtesy ``account_deleted`` email
    is enqueued last; it will bounce against the anonymised inbox —
    that is intentional, it confirms the delete completed for any
    proxied inbox the user gave us.
    """
    import db
    import os as _os
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
    files_removed = 0
    for r in rows:
        user_id = r["id"]
        old_email = r["email"]
        try:
            # ── 1. Collect export ZIP paths on disk BEFORE cascading.
            # ``cascade_delete_user`` will wipe the
            # ``data_export_requests`` row, taking the ``file_path`` with
            # it — read first so we can unlink the ZIPs afterwards.
            export_paths: list[str] = []
            try:
                with db.conn() as c:
                    ex_rows = c.execute(
                        "SELECT file_path FROM data_export_requests "
                        "WHERE user_id = ? AND file_path IS NOT NULL "
                        "AND file_path != ''",
                        (user_id,),
                    ).fetchall()
                    export_paths = [
                        row["file_path"] for row in ex_rows
                    ]
            except Exception:
                # Table may not exist in older snapshots; non-fatal.
                pass

            # ── 2. Anonymise PII on the users row first. If the cascade
            # below crashes mid-flight, the row that remains is still
            # PII-free. The cascade will finalise the users DELETE
            # itself.
            anon_email = f"deleted_{user_id}@deleted.narve.ai"
            with db.conn() as c:
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

            # ── 3. Cascade-delete every user-keyed row across the
            # schema. This covers ~85 (table, column) pairs in the
            # current schema, including every ``*_user_id`` variant
            # (follower_user_id, referrer_user_id, admin_user_id,
            # sharer_user_id, etc.). The function ends by DELETEing
            # the users row itself.
            db.cascade_delete_user(user_id)

            # ── 4. Unlink export ZIP files on disk. GDPR Art. 17 is
            # explicit that "without undue delay" includes ancillary
            # personal data, so the bundled ZIPs go too. Tolerate
            # FileNotFoundError so re-runs and already-cleaned paths
            # don't trip the loop.
            for path in export_paths:
                try:
                    _os.unlink(path)
                    files_removed += 1
                except FileNotFoundError:
                    pass
                except OSError as e:
                    log.warning(
                        "scheduled-deletion: unlink %s: %s", path, e,
                    )

            # ── 5. Final courtesy email — will likely bounce, that is
            # the point. We send to the ORIGINAL (pre-anonymisation)
            # address so any proxied inbox the user gave us still
            # receives the deletion confirmation.
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
            log.exception(
                "hard-delete failed for user_id=%s: %s", user_id, e,
            )

    return {
        "deleted": deleted,
        "checked": len(rows),
        "files_removed": files_removed,
    }


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


# Fix C: run_backtest moved to jobs/backtest_jobs.py - that module is
# the canonical owner (backtest_routes imports it directly). The
# previous in-line copy here registered the same job name at import
# time and silently overwrote whichever module loaded second. With the
# duplicate guard now active in jobs.registry.register_job, importing
# both would raise.


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
