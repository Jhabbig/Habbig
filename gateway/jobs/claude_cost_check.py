"""Daily Claude-spend alert.

Runs every day at 00:05 UTC (after yesterday's day has closed). Totals
yesterday's ``claude_usage_log`` and alerts if it exceeds the threshold
(default $50, overridable via ``CLAUDE_DAILY_SPEND_THRESHOLD_USD``).

Alert delivery:
  1. Always logs at ERROR level (picked up by BetterStack / admin panel).
  2. Writes an audit-log row via security.audit if available.
  3. Tries to enqueue an admin email via the existing email_jobs path —
     graceful no-op if that stack isn't present.

Never raises.
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


log = logging.getLogger("jobs.claude_cost_check")


DEFAULT_THRESHOLD = float(os.environ.get("CLAUDE_DAILY_SPEND_THRESHOLD_USD", "50"))
KILL_SWITCH_THRESHOLD = float(os.environ.get("CLAUDE_KILL_SWITCH_THRESHOLD_USD", "200"))
ADMIN_EMAILS = [
    e.strip() for e in os.environ.get(
        "CLAUDE_COST_ALERT_EMAILS",
        "julian.habbig@icloud.com,shocakarel@gmail.com",
    ).split(",") if e.strip()
]


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent / p)
    return Path(__file__).parent.parent / "auth.db"


@register_job("check_daily_claude_spend")
async def check_daily_claude_spend() -> dict[str, Any]:
    yesterday = (_dt.datetime.utcnow() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        return {"error": f"db open failed: {exc}"}

    try:
        rows = conn.execute(
            """
            SELECT feature,
                   COUNT(*) AS calls,
                   SUM(cached_hit) AS cache_hits,
                   SUM(cost_usd) AS cost_usd
            FROM claude_usage_log
            WHERE strftime('%Y-%m-%d', timestamp, 'unixepoch') = ?
            GROUP BY feature
            """,
            (yesterday,),
        ).fetchall()
    except sqlite3.Error as exc:
        return {"error": f"claude_usage_log read failed: {exc}"}
    finally:
        conn.close()

    by_feature = {
        r["feature"]: {
            "calls": int(r["calls"] or 0),
            "cache_hits": int(r["cache_hits"] or 0),
            "cost_usd": float(r["cost_usd"] or 0.0),
        }
        for r in rows
    }
    total_cost = round(sum(f["cost_usd"] for f in by_feature.values()), 4)
    over = total_cost > DEFAULT_THRESHOLD

    kill_switch_tripped = False
    if over:
        log.error(
            "claude daily spend alert: day=%s cost_usd=%.4f threshold=%.2f breakdown=%s",
            yesterday, total_cost, DEFAULT_THRESHOLD, by_feature,
        )
        _record_alert(yesterday, total_cost, DEFAULT_THRESHOLD)
        _audit_log(yesterday, total_cost, by_feature)
        await _try_enqueue_email(yesterday, total_cost, by_feature)
    else:
        log.info("claude daily spend OK: day=%s cost_usd=%.4f", yesterday, total_cost)

    if total_cost > KILL_SWITCH_THRESHOLD:
        log.critical(
            "claude kill-switch TRIPPED: day=%s cost_usd=%.4f > %.2f — blocking uncached calls",
            yesterday, total_cost, KILL_SWITCH_THRESHOLD,
        )
        _record_alert(yesterday, total_cost, KILL_SWITCH_THRESHOLD)
        try:
            from ai.client import set_kill_switch  # type: ignore
            set_kill_switch(
                active=True,
                reason=f"Auto-trip: ${total_cost:.2f} on {yesterday}",
                triggered_by="cost_check_job",
            )
            kill_switch_tripped = True
        except Exception as exc:
            log.exception("set_kill_switch failed: %s", exc)

    return {
        "day": yesterday,
        "cost_usd": total_cost,
        "threshold_usd": DEFAULT_THRESHOLD,
        "over_threshold": over,
        "kill_switch_tripped": kill_switch_tripped,
        "by_feature": by_feature,
    }


def _record_alert(day: str, cost_usd: float, threshold: float) -> None:
    """Log an alert row, dedupe on (day, threshold) so re-runs are idempotent."""
    try:
        conn = sqlite3.connect(_db_path())
    except Exception:
        return
    try:
        conn.execute(
            "INSERT OR IGNORE INTO claude_cost_alerts "
            "(alert_date, threshold_usd, total_cost_usd, sent_at) VALUES (?, ?, ?, ?)",
            (day, float(threshold), float(cost_usd), int(time.time())),
        )
        conn.commit()
    except sqlite3.Error as exc:
        log.warning("claude_cost_alerts insert failed: %s", exc)
    finally:
        conn.close()


def _audit_log(day: str, total_cost: float, breakdown: dict) -> None:
    try:
        from security import audit as _audit  # type: ignore
        _audit.log_action(
            admin_user_id=0,
            admin_email="system",
            action=getattr(_audit.AuditAction, "SYSTEM_ALERT", "system_alert"),
            target_type="claude_spend",
            target_id=day,
            target_description=f"${total_cost:.2f} > ${DEFAULT_THRESHOLD:.2f}",
            after={"day": day, "cost_usd": total_cost, "by_feature": breakdown},
        )
    except Exception:
        pass


async def _try_enqueue_email(day: str, total_cost: float, breakdown: dict) -> None:
    try:
        from jobs.email_jobs import enqueue_email  # type: ignore
    except ImportError:
        return
    for addr in ADMIN_EMAILS:
        try:
            await enqueue_email(
                to=addr,
                template="admin_cost_alert",
                context={
                    "day": day,
                    "cost_usd": total_cost,
                    "threshold": DEFAULT_THRESHOLD,
                    "breakdown": breakdown,
                },
                tags=["admin_alert"],
            )
        except Exception as exc:
            log.warning("claude spend alert email to %s failed: %s", addr, exc)


register_cron("check_daily_claude_spend", hour=0, minute=5)
