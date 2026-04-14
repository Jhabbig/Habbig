"""
Background mark-to-market worker for the gateway.

Periodically:
  1. Fetches open positions that need a price update
  2. Batches them by (platform, external_id) to avoid duplicate API calls
  3. Pulls live mid-prices from each platform
  4. Updates last_mark_price in the user_positions table

Runs as an asyncio task alongside the Poller, started from server.py.
"""

from __future__ import annotations

import asyncio
import logging
import time

import db
import trading

log = logging.getLogger("gateway.mtm")

# How often to run a mark cycle (seconds)
MARK_INTERVAL = 30

# Max positions to mark per cycle (prevents thundering herd)
BATCH_SIZE = 200


class MarkToMarketWorker:
    """Async background worker that keeps open positions priced."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_cycle: float = 0
        self._last_marked: int = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="mark-to-market")
        log.info("Mark-to-market worker started (interval=%ds, batch=%d)", MARK_INTERVAL, BATCH_SIZE)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("Mark-to-market worker stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._cycle()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Mark-to-market cycle failed")
            await asyncio.sleep(MARK_INTERVAL)

    async def _cycle(self) -> None:
        """One mark-to-market cycle."""
        t0 = time.monotonic()
        rows = db.get_positions_needing_mark(limit=BATCH_SIZE)
        if not rows:
            return

        # Deduplicate: multiple users may hold the same market
        # Key: (platform, external_id) → list of position IDs
        market_positions: dict[tuple[str, str, str], list[int]] = {}
        for r in rows:
            key = (r["platform"], r["external_id"], r["token_or_side"])
            market_positions.setdefault(key, []).append(r["id"])

        # Fetch prices in parallel (one call per unique market)
        tasks = []
        keys = []
        for (platform, ext_id, token_or_side) in market_positions:
            tasks.append(trading.get_mark_price(platform, ext_id, token_or_side))
            keys.append((platform, ext_id, token_or_side))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        marked = 0
        for key, result in zip(keys, results):
            if isinstance(result, Exception):
                log.debug("Mark price failed for %s/%s: %s", key[0], key[1], result)
                continue
            if result is None:
                continue
            price = float(result)
            for pos_id in market_positions[key]:
                db.update_mark_price(pos_id, price)
                marked += 1

        elapsed = time.monotonic() - t0
        self._last_cycle = time.time()
        self._last_marked = marked
        if marked > 0:
            log.info(
                "Marked %d positions (%d markets) in %.1fs",
                marked, len(market_positions), elapsed,
            )

    def stats(self) -> dict:
        return {
            "running": self._running,
            "last_cycle": self._last_cycle,
            "last_marked": self._last_marked,
            "interval": MARK_INTERVAL,
            "batch_size": BATCH_SIZE,
        }
