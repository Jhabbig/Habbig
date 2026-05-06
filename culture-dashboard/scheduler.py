"""Background refresh loop.

Runs every scraper on its own cadence. Failures are isolated per-source so
one broken scraper never takes the dashboard down.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

import cache
import dedup
import index_calc
from models import Item

log = logging.getLogger(__name__)

# (source_name, async_fetcher, refresh_seconds)
ScraperSpec = tuple[str, Callable[[], Awaitable[list[Item]]], int]


class Scheduler:
    def __init__(self, specs: list[ScraperSpec]) -> None:
        self._specs = specs
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._next_run: dict[str, float] = {name: 0.0 for name, _, _ in specs}

    async def start(self) -> None:
        if self._task is not None:
            return
        cache.init_db()
        self._task = asyncio.create_task(self._loop(), name="culture-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def run_once(self, only: str | None = None) -> None:
        """Force-run all scrapers (or one). Useful for /api/refresh."""
        await asyncio.gather(*[
            self._run_one(name, fetch)
            for name, fetch, _ in self._specs
            if only is None or only == name
        ])

    async def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            due = [(n, f, p) for n, f, p in self._specs if self._next_run.get(n, 0) <= now]
            if due:
                await asyncio.gather(*[self._run_one(n, f) for n, f, _ in due])
                for n, _, period in due:
                    self._next_run[n] = time.time() + period
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _run_one(self, name: str, fetch: Callable[[], Awaitable[list[Item]]]) -> None:
        try:
            items = await fetch()
            cache.replace_source(name, items or [])
            log.info("refreshed %s: %d items", name, len(items or []))
        except Exception as e:  # noqa: BLE001 — isolate scraper failures
            log.warning("scraper %s failed: %s", name, e)
            cache.record_failure(name, str(e))


async def phash_worker(stop: asyncio.Event, period: int = 120) -> None:
    """Background worker that hashes any newly-arrived images."""
    while not stop.is_set():
        try:
            await dedup.compute_missing_phashes(limit=30)
        except Exception as e:  # noqa: BLE001
            log.warning("phash worker hiccup: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=period)
        except asyncio.TimeoutError:
            pass


async def index_history_worker(stop: asyncio.Event, period: int = 600) -> None:
    """Snapshot the composite index every `period` seconds (default 10 min)."""
    import json as _json
    while not stop.is_set():
        try:
            snap = index_calc.compute()
            cache.record_index_snapshot(
                snap.get("overall"),
                _json.dumps(snap.get("sections", {})),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("index snapshot failed: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=period)
        except asyncio.TimeoutError:
            pass
