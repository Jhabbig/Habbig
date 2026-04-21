"""Scheduled portfolio sync jobs (Polymarket every 10m, Kalshi every 15m).

Both jobs iterate every active connection and call the platform's sync
function. Errors are contained per-user so a single bad token doesn't
stall the batch.

Kalshi sync skips connections whose last error was 401 (token expired) —
those need a manual re-login. The admin panel surfaces the count so we
can nudge users.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.sync_portfolios")


# Don't keep retrying connections that have failed many times in a row —
# the 10-minute sync cadence would waste the platform's rate budget.
_MAX_ERROR_STREAK = 10


@register_job("sync_polymarket_positions")
async def sync_polymarket_positions_job() -> dict[str, Any]:
    import db
    from portfolio import polymarket

    with db.conn() as c:
        rows = c.execute(
            "SELECT user_id, sync_error_count FROM polymarket_connections"
        ).fetchall()

    synced = 0
    skipped = 0
    errors = 0
    start = time.monotonic()
    for row in rows:
        if (row["sync_error_count"] or 0) >= _MAX_ERROR_STREAK:
            skipped += 1
            continue
        try:
            result = await polymarket.sync_positions(int(row["user_id"]))
            if result.get("error"):
                errors += 1
            else:
                synced += 1
        except Exception as exc:
            log.exception("polymarket sync crashed user=%s: %s",
                          row["user_id"], exc)
            errors += 1
        # Tiny yield between users so the event loop stays responsive.
        await asyncio.sleep(0)

    duration = round(time.monotonic() - start, 2)
    log.info(
        "polymarket sync: %d synced, %d errors, %d skipped, %.2fs",
        synced, errors, skipped, duration,
    )
    return {"synced": synced, "errors": errors, "skipped": skipped,
            "duration_seconds": duration}


@register_job("sync_kalshi_positions")
async def sync_kalshi_positions_job() -> dict[str, Any]:
    import db
    from portfolio import kalshi

    with db.conn() as c:
        rows = c.execute(
            "SELECT user_id, sync_error, sync_error_count "
            "FROM kalshi_connections"
        ).fetchall()

    synced = 0
    skipped_auth = 0
    skipped_streak = 0
    errors = 0
    start = time.monotonic()
    for row in rows:
        if (row["sync_error_count"] or 0) >= _MAX_ERROR_STREAK:
            skipped_streak += 1
            continue
        if row["sync_error"] and row["sync_error"].startswith("HTTP 401"):
            # Token expired — needs re-connect. Skip quietly.
            skipped_auth += 1
            continue
        try:
            result = await kalshi.sync_positions(int(row["user_id"]))
            if result.get("error"):
                errors += 1
            else:
                synced += 1
        except Exception as exc:
            log.exception("kalshi sync crashed user=%s: %s",
                          row["user_id"], exc)
            errors += 1
        await asyncio.sleep(0)

    duration = round(time.monotonic() - start, 2)
    log.info(
        "kalshi sync: %d synced, %d errors, %d stale-auth, %d streak-skipped, %.2fs",
        synced, errors, skipped_auth, skipped_streak, duration,
    )
    return {
        "synced": synced, "errors": errors,
        "skipped_auth": skipped_auth, "skipped_streak": skipped_streak,
        "duration_seconds": duration,
    }


# arq-cron doesn't support "every N minutes" directly; we schedule at
# each minute slot explicitly. Polymarket every 10 minutes, Kalshi
# every 15, offset so they don't collide.
for _m in (1, 11, 21, 31, 41, 51):
    register_cron("sync_polymarket_positions", minute=_m)
for _m in (3, 18, 33, 48):
    register_cron("sync_kalshi_positions", minute=_m)
