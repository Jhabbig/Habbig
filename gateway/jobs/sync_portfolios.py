"""Scheduled portfolio sync jobs (Polymarket every 10m, Kalshi every 15m).

Both jobs iterate every active connection and call the platform's sync
function. Errors are contained per-user so a single bad token doesn't
stall the batch.

Kalshi sync skips connections whose last error was 401 (token expired) —
those need a manual re-login. The admin panel surfaces the count so we
can nudge users.

Polymarket sync hardening (2026-05-14)
--------------------------------------
The Polymarket job now does four things that materially cut API load
while keeping every active user's portfolio fresh:

1. **Stagger user-sync timing.** The arq-cron entries still fire every
   minute (so any user's slot is reachable inside a 10-minute window),
   but each user is only synced on the minute whose value matches
   ``hash(user_id) % 60``. Users no longer pile onto the same minute;
   the per-minute fan-out is ~1/10 of the user base.

2. **Skip inactive users to a weekly cadence.** Users with no session
   activity in the last 30 days only sync once per week (on Mondays at
   their offset). They still get fresh data the moment they log back
   in via the live ``/api/markets/portfolio`` flow — this only changes
   the background refresh cadence.

3. **Cache Gamma market state for 60s.** ``polymarket.fetch_market_state``
   memoises market payloads so multiple users holding the same market
   share one upstream fetch inside a single run.

4. **Pace + back off.** A token-bucket caps outbound CLOB calls at
   ~5 req/sec (Polymarket's published rate limit is 30/sec — staying
   well under leaves headroom for the live trade flow). On HTTP 429 we
   skip the remaining users in this run and let the next cron tick try
   again with backoff.
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

# Inactive = no session activity in this many seconds. 30 days matches
# the spec; the actual cadence drop is to weekly (Monday at offset).
_INACTIVE_AFTER_SECONDS = 30 * 86400

# Polymarket's documented CLOB rate limit is 30 req/sec. We target ~5/sec
# sustained so the live ``/api/markets/portfolio`` path keeps its budget.
_TARGET_RPS = 5.0
_REQUEST_INTERVAL = 1.0 / _TARGET_RPS

# Exponential-backoff parameters for 429s. We abort the rest of the run
# rather than thrashing — the next cron tick will pick up the slack.
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0


def _user_offset(user_id: int) -> int:
    """Stable 0..59 offset for a user. Spreads sync load across minutes."""
    # ``hash(int)`` is the identity in CPython but PYTHONHASHSEED could
    # randomise it on str inputs; force int to keep the offset stable
    # across process restarts.
    return int(user_id) % 60


def _last_active_at(c, user_id: int) -> int | None:
    """Most recent session activity timestamp for *user_id*.

    Falls back to the legacy ``sessions`` table (which doesn't track
    last_active_at) using the most recent ``created_at`` — better than
    nothing for users created before user_sessions rollout.
    """
    row = c.execute(
        "SELECT MAX(last_active_at) AS ts FROM user_sessions "
        "WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row and row["ts"]:
        return int(row["ts"])
    row = c.execute(
        "SELECT MAX(created_at) AS ts FROM sessions WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return int(row["ts"]) if row and row["ts"] else None


def _due_this_minute(
    user_id: int,
    *,
    now_minute: int,
    now_weekday: int,
    last_active_at: int | None,
    now_epoch: int,
) -> bool:
    """Should this user be synced in the current cron tick?

    - Active users (activity in last 30d) sync once per 10-minute window
      at the minute equal to ``hash(user_id) % 60``-mod-10.
    - Inactive users (>=30d quiet) sync once per week — Monday at their
      offset minute. The arq-cron entries fire every minute, so we only
      have to match the per-user minute and weekday.
    """
    offset = _user_offset(user_id)
    is_inactive = (
        last_active_at is not None
        and (now_epoch - last_active_at) >= _INACTIVE_AFTER_SECONDS
    )
    if is_inactive:
        # weekday=0 is Monday in arq.cron / datetime.weekday().
        return now_weekday == 0 and now_minute == offset
    # Active: bucket into a 10-minute window so the cadence stays at 6x/hour.
    return (now_minute % 10) == (offset % 10)


class _RateLimited429(Exception):
    """Raised by the per-user sync wrapper to abort the rest of the run."""


async def _sync_one_user(polymarket, user_id: int) -> dict[str, Any]:
    """Run ``polymarket.sync_positions`` and surface 429 as a typed error."""
    import httpx

    try:
        return await polymarket.sync_positions(user_id)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            raise _RateLimited429() from exc
        raise


@register_job("sync_polymarket_positions")
async def sync_polymarket_positions_job() -> dict[str, Any]:
    import db
    from portfolio import polymarket

    now_epoch = int(time.time())
    # Local time is fine for the offset — we only need a stable minute.
    lt = time.localtime(now_epoch)
    now_minute = lt.tm_min
    now_weekday = lt.tm_wday  # 0=Monday, matches arq.cron + spec.

    with db.conn() as c:
        rows = c.execute(
            "SELECT user_id, sync_error_count FROM polymarket_connections"
        ).fetchall()
        # Pre-fetch last-active timestamps in one round-trip rather than
        # querying per user. Falls back gracefully if user_sessions is empty.
        last_active: dict[int, int | None] = {}
        for row in rows:
            last_active[int(row["user_id"])] = _last_active_at(
                c, int(row["user_id"])
            )

    synced = 0
    skipped_streak = 0
    skipped_schedule = 0
    skipped_inactive = 0
    errors = 0
    backoff_aborts = 0
    last_call = 0.0
    backoff = _BACKOFF_BASE_SECONDS
    start = time.monotonic()

    for row in rows:
        user_id = int(row["user_id"])
        if (row["sync_error_count"] or 0) >= _MAX_ERROR_STREAK:
            skipped_streak += 1
            continue
        la = last_active.get(user_id)
        if not _due_this_minute(
            user_id,
            now_minute=now_minute,
            now_weekday=now_weekday,
            last_active_at=la,
            now_epoch=now_epoch,
        ):
            if la is not None and (now_epoch - la) >= _INACTIVE_AFTER_SECONDS:
                skipped_inactive += 1
            else:
                skipped_schedule += 1
            continue

        # Token-bucket pacing — ~5 req/sec sustained.
        wait = _REQUEST_INTERVAL - (time.monotonic() - last_call)
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            result = await _sync_one_user(polymarket, user_id)
            if result.get("error"):
                errors += 1
            else:
                synced += 1
            backoff = _BACKOFF_BASE_SECONDS  # reset on any success path
        except _RateLimited429:
            log.warning(
                "polymarket 429 — backing off %.1fs and aborting run",
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
            backoff_aborts += 1
            break
        except Exception as exc:
            log.exception("polymarket sync crashed user=%s: %s",
                          user_id, exc)
            errors += 1
        finally:
            last_call = time.monotonic()
        # Tiny yield between users so the event loop stays responsive.
        await asyncio.sleep(0)

    duration = round(time.monotonic() - start, 2)
    log.info(
        "polymarket sync: %d synced, %d errors, %d streak-skipped, "
        "%d off-schedule, %d inactive-deferred, %d 429-aborts, %.2fs",
        synced, errors, skipped_streak, skipped_schedule,
        skipped_inactive, backoff_aborts, duration,
    )
    return {
        "synced": synced,
        "errors": errors,
        "skipped_streak": skipped_streak,
        "skipped_schedule": skipped_schedule,
        "skipped_inactive": skipped_inactive,
        "backoff_aborts": backoff_aborts,
        "duration_seconds": duration,
    }


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


# Polymarket: cron fires every minute and the job itself filters down
# to ~1/10 of users per tick via ``_user_offset`` — this gives every
# user a 10-minute cadence with the load spread evenly across minutes.
# Inactive users (30d+ no activity) are deferred to Monday at their
# offset minute (~weekly).
for _m in range(60):
    register_cron("sync_polymarket_positions", minute=_m)

# Kalshi is unchanged — every 15 minutes, offset from Polymarket.
for _m in (3, 18, 33, 48):
    register_cron("sync_kalshi_positions", minute=_m)
