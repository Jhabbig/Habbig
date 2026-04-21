"""Weekly PDF report generation for Pro users.

Runs every Monday at 07:00 UTC — one hour ahead of the 08:00 digest
email batch, so the PDF is ready to attach.

Per Pro user with email_digest=1:
  reports.build_report_for_user(user_id, period_start, period_end)

Then attempts to enqueue an email with the PDF path via existing
email_jobs.enqueue_email. Missing email plumbing → the cron still
generates + stores the PDF so /reports/weekly/<id>/pdf works.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.weekly_reports")


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent / p)
    return Path(__file__).parent.parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _eligible_users() -> list[dict]:
    conn = _connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        digest_col = "email_digest" if "email_digest" in cols else None
        # Pro detection — most branches have a `__plan__` sentinel row in
        # subscriptions. Tolerant read:
        rows = conn.execute(
            "SELECT u.id, u.email "
            "FROM users u "
            "JOIN subscriptions s ON s.user_id = u.id AND s.dashboard_key = '__plan__' "
            "WHERE s.status = 'active' "
            "AND (s.expires_at IS NULL OR s.expires_at > ?) "
            "AND COALESCE(u.is_deleted, 0) = 0"
            + (f" AND COALESCE(u.{digest_col}, 1) = 1" if digest_col else ""),
            (int(time.time()),),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        log.warning("weekly reports: user query failed: %s", exc)
        return []
    finally:
        conn.close()


@register_job("generate_weekly_reports")
async def generate_weekly_reports() -> dict[str, Any]:
    from reports.weekly import build_report_for_user  # lazy import

    now = _dt.datetime.utcnow()
    # Week = last full Mon→Sun, UTC. Generated on Monday 07:00.
    period_end = int((now - _dt.timedelta(days=now.weekday() % 7)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).timestamp())
    period_start = period_end - 7 * 86400

    users = _eligible_users()
    log.info("weekly reports: %d eligible Pro users", len(users))
    generated = 0
    skipped = 0
    failed = 0
    emailed = 0

    for user in users:
        try:
            result = await build_report_for_user(user["id"], period_start, period_end)
            status = result.get("status")
            if status in ("ready", "ready_html_only"):
                generated += 1
                if await _try_enqueue_email(user, result):
                    emailed += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
        except Exception:
            log.exception("weekly report failed for user %d", user["id"])
            failed += 1

    return {
        "period_start": period_start,
        "period_end": period_end,
        "eligible": len(users),
        "generated": generated,
        "skipped": skipped,
        "failed": failed,
        "emailed": emailed,
    }


async def _try_enqueue_email(user: dict, report: dict) -> bool:
    try:
        from jobs.email_jobs import enqueue_email  # type: ignore
    except ImportError:
        return False
    try:
        await enqueue_email(
            to=user["email"],
            template="weekly_intelligence",
            context={
                "report_id": report.get("report_id"),
                "pdf_path": report.get("pdf_path"),
                "stats": report.get("stats"),
            },
            tags=["weekly_intelligence"],
        )
        return True
    except Exception as exc:
        log.warning("weekly email enqueue failed for %s: %s", user.get("email"), exc)
        return False


register_cron("generate_weekly_reports", weekday=0, hour=7, minute=0)
