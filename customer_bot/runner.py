"""Background poller orchestrating all sources.

Wired into the gateway's FastAPI startup (see gateway/server.py). Sleeps
POLL_INTERVAL seconds between cycles, then asks every source for fresh
posts, scores them, and writes matching leads to the shared SQLite db.

No outbound writes to any social platform. Only reads.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from customer_bot import store
from customer_bot.config import ALL_SUBREDDITS, TOPICS, topic_for_text
from customer_bot.drafter import draft_for, score_for
from customer_bot.lead import RawLead
from customer_bot.sources import hn as hn_source
from customer_bot.sources import polymarket as pm_source
from customer_bot.sources import reddit as reddit_source

log = logging.getLogger("customer_bot.runner")

POLL_INTERVAL_SEC = 60 * 30   # 30 minutes between full cycles
MIN_SCORE = 25                # below this, drop the lead


class LeadsPoller:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._running = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="customer_bot.poller")
        log.info("LeadsPoller started — interval=%ds", POLL_INTERVAL_SEC)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _loop(self) -> None:
        # Small initial delay so we don't compete with gateway boot.
        await asyncio.sleep(15)
        while self._running:
            try:
                store.unsnooze_expired()
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                log.exception("LeadsPoller cycle failed: %s", exc)
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                raise

    async def _cycle(self) -> None:
        assert self._client is not None
        inserted = 0

        # 1. Reddit — one subreddit at a time, pause 1s between to be polite.
        for sub in ALL_SUBREDDITS:
            async for raw in reddit_source.fetch(self._client, sub, limit=25):
                if self._ingest(raw):
                    inserted += 1
            await asyncio.sleep(1.0)

        # 2. HN — one query per topic.
        for topic in TOPICS:
            async for raw in hn_source.fetch(self._client, topic.hn_query, limit=15):
                if self._ingest(raw, hinted_topic=topic):
                    inserted += 1
            await asyncio.sleep(0.5)

        # 3. Polymarket — pooled keywords across all topics.
        all_keywords: tuple[str, ...] = tuple({kw for t in TOPICS for kw in t.keywords})
        async for raw in pm_source.fetch(self._client, all_keywords, limit=30):
            if self._ingest(raw):
                inserted += 1

        log.info("LeadsPoller cycle complete — %d new leads", inserted)

    def _ingest(self, raw: RawLead, hinted_topic=None) -> bool:
        text = f"{raw.title}\n{raw.body}"
        topic = hinted_topic or topic_for_text(text)
        if topic is None:
            return False
        score = score_for(raw, topic)
        if score < MIN_SCORE:
            return False
        draft = draft_for(raw, topic)
        snippet = (raw.body or raw.title)[:400]
        return store.upsert_lead(
            source=raw.source,
            source_id=raw.source_id,
            url=raw.url,
            author=raw.author,
            title=raw.title[:300],
            snippet=snippet,
            dashboard_key=topic.key,
            score=score,
            draft=draft,
            posted_at=raw.posted_at or int(time.time()),
        )
