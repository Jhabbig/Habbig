"""Background refresh loop.

Runs every scraper on its own cadence. Failures are isolated per-source so
one broken scraper never takes the dashboard down.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import time
from typing import Awaitable, Callable

import httpx

import cache
import dedup
import digest
import edge as edge_mod
import headlines
import index_calc
import surge_calc
import time as _time
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


async def surge_worker(stop: asyncio.Event, period: int = 300) -> None:
    """Detect surges, fire webhooks (with cooldown), prune old history."""
    webhook = os.environ.get("SURGE_WEBHOOK_URL", "").strip()
    threshold = surge_calc.webhook_threshold()
    cooldown = surge_calc.cooldown_seconds()
    while not stop.is_set():
        try:
            surges = surge_calc.compute(limit=50)
            if webhook:
                for s in surges:
                    if s["z_score"] < threshold:
                        continue
                    if cache.recent_alert(s["source"], s["key"], cooldown):
                        continue
                    await _fire_webhook(webhook, s)
                    cache.record_alert(
                        s["source"], s["key"], s["z_score"],
                        _json.dumps({k: v for k, v in s.items() if k != "trajectory"}),
                    )
            removed = cache.prune_history(days=7)
            if removed:
                log.info("pruned %d old item_history rows", removed)
            removed_prices = cache.prune_market_prices(days=30)
            if removed_prices:
                log.info("pruned %d old market_prices rows", removed_prices)
            removed_topics = cache.prune_topic_snapshots(days=30)
            if removed_topics:
                log.info("pruned %d old topic_snapshots rows", removed_topics)
            # Snapshot active cross-source topic clusters for backtesting.
            try:
                snaps = []
                for t in edge_mod.compute_topics_with_markets(limit=50):
                    if t.get("surge_signal") is None or t["surge_signal"] < 1.0:
                        # Only retain meaningfully-signalled topics — others would
                        # balloon the snapshot table without informing backtests.
                        if t["spread"] < 4:
                            continue
                    snaps.append({
                        "ts": _time.time(),
                        "label": t["label"],
                        "spread": t["spread"],
                        "surge_signal": t.get("surge_signal"),
                        "sources": t["sources"],
                        "sections": t["sections"],
                        "market_slugs": [m["event_slug"] for m in t["markets"]
                                         if m.get("event_slug")],
                    })
                if snaps:
                    cache.record_topic_snapshots(snaps)
                    log.info("recorded %d topic snapshots", len(snaps))
            except Exception as e:  # noqa: BLE001
                log.warning("topic snapshot hiccup: %s", e)
            try:
                headlines.write_today()
            except Exception as e:  # noqa: BLE001
                log.warning("daily headline write failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("surge worker hiccup: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=period)
        except asyncio.TimeoutError:
            pass


async def digest_worker(stop: asyncio.Event, period: int | None = None) -> None:
    """Regenerate the LLM culture digest on a fixed cadence."""
    if period is None:
        try:
            period = int(os.environ.get("CULTURE_DIGEST_INTERVAL", "3600"))
        except ValueError:
            period = 3600
    while not stop.is_set():
        try:
            d = await asyncio.to_thread(digest.generate)
            if d:
                cache.record_digest(d)
                log.info(
                    "digest refreshed via %s (%d in / %d out / %d cached read)",
                    d["model"], d["input_tokens"], d["output_tokens"],
                    d["cache_read_tokens"],
                )
                # Fire downstream webhook (Slack/Discord/raw JSON), if configured.
                if os.environ.get("DIGEST_WEBHOOK_URL", "").strip():
                    if await digest.fire_webhook(d):
                        log.info("digest pushed to DIGEST_WEBHOOK_URL")
        except Exception as e:  # noqa: BLE001
            log.warning("digest generation failed: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=period)
        except asyncio.TimeoutError:
            pass


async def _fire_webhook(url: str, surge: dict) -> None:
    payload = {
        "type": "culture.surge",
        "source": surge.get("source"),
        "section": surge.get("section"),
        "title": surge.get("title"),
        "url": surge.get("url"),
        "z_score": surge.get("z_score"),
        "score": surge.get("score"),
        "ts": time.time(),
    }
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(url, json=payload)
    except Exception as e:  # noqa: BLE001
        log.warning("surge webhook POST failed: %s", e)
