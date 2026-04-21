"""Portfolio sync crons for Polymarket and Kalshi.

Schedules:
  - sync_polymarket_portfolios  → every 10 minutes
  - sync_kalshi_portfolios      → every 15 minutes
  - refresh_position_prices     → every 2 minutes

The 10/15-min jobs hit each exchange's positions API for every user with
an active connection. The 2-min price refresh re-uses the already-cached
unified market list to update current_price/value/pnl on persisted rows
without hitting either positions API — cheap enough to run frequently.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from jobs.registry import register_job, register_cron

log = logging.getLogger("jobs.portfolio")


def _poly_client():
    from backend.markets.polymarket_client import PolymarketClient
    return PolymarketClient()


def _kalshi_client():
    from backend.markets.kalshi_client import KalshiClient
    return KalshiClient(
        base_url=os.environ.get(
            "KALSHI_API_BASE",
            "https://trading-api.kalshi.com/trade-api/v2",
        ),
    )


@register_job("sync_polymarket_portfolios")
async def sync_polymarket_portfolios() -> dict[str, Any]:
    from backend.markets import unified_markets
    from backend.markets.portfolio_sync import (
        list_active_user_ids, sync_user_portfolio,
    )

    user_ids = list_active_user_ids("polymarket")
    if not user_ids:
        return {"synced": 0, "platform": "polymarket"}

    poly = _poly_client()
    kalshi = _kalshi_client()
    synced = 0
    errors = 0
    try:
        for uid in user_ids:
            try:
                await sync_user_portfolio(
                    uid,
                    poly_client=poly,
                    kalshi_client=kalshi,
                    unified_markets_module=unified_markets,
                    only_platform="polymarket",
                )
                synced += 1
            except Exception as exc:
                errors += 1
                log.warning("polymarket sync failed for user %d: %s", uid, exc)
    finally:
        await poly.close()
        await kalshi.close()
    return {"synced": synced, "errors": errors, "platform": "polymarket"}


@register_job("sync_kalshi_portfolios")
async def sync_kalshi_portfolios() -> dict[str, Any]:
    from backend.markets import unified_markets
    from backend.markets.portfolio_sync import (
        list_active_user_ids, sync_user_portfolio,
    )

    user_ids = list_active_user_ids("kalshi")
    if not user_ids:
        return {"synced": 0, "platform": "kalshi"}

    poly = _poly_client()
    kalshi = _kalshi_client()
    synced = 0
    errors = 0
    try:
        for uid in user_ids:
            try:
                await sync_user_portfolio(
                    uid,
                    poly_client=poly,
                    kalshi_client=kalshi,
                    unified_markets_module=unified_markets,
                    only_platform="kalshi",
                )
                synced += 1
            except Exception as exc:
                errors += 1
                log.warning("kalshi sync failed for user %d: %s", uid, exc)
    finally:
        await poly.close()
        await kalshi.close()
    return {"synced": synced, "errors": errors, "platform": "kalshi"}


@register_job("refresh_position_prices")
async def refresh_position_prices() -> dict[str, Any]:
    """Update current_price / value / pnl on every cached position from the
    shared unified markets snapshot. Does NOT re-fetch from the positions
    API — that's what the 10/15-min jobs do."""
    import db
    from backend.markets import unified_markets
    from backend.markets.portfolio_sync import refresh_prices_only

    poly = _poly_client()
    kalshi = _kalshi_client()
    try:
        markets = await unified_markets.fetch_unified_markets(
            poly, kalshi, cache_ttl=120,
        )
    finally:
        await poly.close()
        await kalshi.close()

    price_map = {m.id: m.yes_price for m in markets}

    with db.conn() as c:
        rows = c.execute(
            "SELECT DISTINCT user_id FROM user_positions"
        ).fetchall()

    total_updated = 0
    for row in rows:
        total_updated += refresh_prices_only(int(row["user_id"]), price_map)
    return {"users": len(rows), "positions_updated": total_updated}


# ── Schedules ─────────────────────────────────────────────────────────────
# register_cron fires on exact minute matches, so "every N minutes" is
# implemented by registering the job at each N-minute mark in the hour.

for _m in range(0, 60, 10):
    register_cron("sync_polymarket_portfolios", minute=_m)

for _m in range(0, 60, 15):
    register_cron("sync_kalshi_portfolios", minute=_m)

for _m in range(0, 60, 2):
    register_cron("refresh_position_prices", minute=_m)
