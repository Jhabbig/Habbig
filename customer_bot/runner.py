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

from customer_bot import email_out, llm, store
from customer_bot.config import ALL_SUBREDDITS, TOPICS, topic_by_key, topic_for_text
from customer_bot.drafter import draft_for, ref_code, score_for
from customer_bot.email_extract import extract_email
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
                archived = store.archive_stale_new(days=21)
                if archived:
                    log.info("LeadsPoller archived %d stale 'new' leads", archived)
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
        inserted = rejected = low_quality = 0

        # Per-cycle conversion lift map — self-tuning from past outcomes.
        lift_map = store.signed_up_lift()
        if lift_map:
            log.info("Conversion lift active for %d (source,dashboard) pairs", len(lift_map))

        # 1. Reddit posts + comments — comments are where prospects actually
        #    ask for tools, so we poll both endpoints per sub.
        for sub in ALL_SUBREDDITS:
            async for raw in reddit_source.fetch(self._client, sub, limit=25):
                got = await self._ingest(raw, lift_map=lift_map)
                inserted += int(got == "ok"); rejected += int(got == "reject"); low_quality += int(got == "low_quality")
            await asyncio.sleep(1.0)
            async for raw in reddit_source.fetch_comments(self._client, sub, limit=50):
                got = await self._ingest(raw, lift_map=lift_map)
                inserted += int(got == "ok"); rejected += int(got == "reject"); low_quality += int(got == "low_quality")
            await asyncio.sleep(1.0)

        # 2. HN — one query per topic.
        for topic in TOPICS:
            async for raw in hn_source.fetch(self._client, topic.hn_query, limit=15):
                got = await self._ingest(raw, hinted_topic=topic, lift_map=lift_map)
                inserted += int(got == "ok"); rejected += int(got == "reject"); low_quality += int(got == "low_quality")
            await asyncio.sleep(0.5)

        # 3. Polymarket — pooled keywords across all topics.
        all_keywords: tuple[str, ...] = tuple({kw for t in TOPICS for kw in t.keywords})
        async for raw in pm_source.fetch(self._client, all_keywords, limit=30):
            got = await self._ingest(raw, lift_map=lift_map)
            inserted += int(got == "ok"); rejected += int(got == "reject"); low_quality += int(got == "low_quality")

        log.info(
            "LeadsPoller cycle complete — %d new, %d rejected (filter), %d dropped (low-quality author)",
            inserted, rejected, low_quality,
        )

        # Auto-email pass — off by default, hard-gated on env var.
        if email_out.autosend_enabled():
            await self._autosend_pass()

    async def _ingest(self, raw: RawLead, hinted_topic=None, lift_map: dict | None = None) -> str:
        """Return 'ok' / 'reject' / 'low_quality' / 'skip'."""
        text = f"{raw.title}\n{raw.body}"
        topic = hinted_topic or topic_for_text(text)
        if topic is None:
            return "skip"
        score = score_for(raw, topic)
        if score < 0:
            return "reject"

        # Apply self-tuning lift from historical outcomes before threshold check.
        if lift_map:
            score = max(0, min(100, score + lift_map.get((raw.source, topic.key), 0)))
        if score < MIN_SCORE:
            return "skip"

        # Reddit author quality gate — drops fresh / low-karma accounts that
        # are almost always bots or karma farmers. Skips for non-Reddit sources.
        if raw.source in ("reddit", "reddit_comment") and raw.author:
            assert self._client is not None
            info = await reddit_source.fetch_author(self._client, raw.author)
            if not reddit_source.is_quality_author(info):
                return "low_quality"

        # Email discovery — try the post body first, then the author's
        # platform profile (HN only — Reddit profiles never contain emails).
        email = extract_email(raw.body) or extract_email(raw.title) or ""
        if not email and raw.source == "hn" and raw.author:
            assert self._client is not None
            about = await hn_source.fetch_user_about(self._client, raw.author)
            if about:
                email = extract_email(about) or ""

        draft = draft_for(raw, topic)
        if llm.is_available():
            draft = await llm.polish_draft(draft, raw, topic)

        snippet = (raw.body or raw.title)[:400]
        ok = store.upsert_lead(
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
            ref_code=ref_code(raw.source_id),
            email=email,
        )
        return "ok" if ok else "skip"

    async def _autosend_pass(self) -> None:
        """Send cold emails to qualifying leads, throttled by daily cap.

        Hard gates (every send must clear all of these):
          • LEADS_AUTOSEND_ENABLED=1 (already checked by caller)
          • score >= LEADS_AUTOSEND_MIN_SCORE (default 70)
          • lead has a usable email
          • recipient not on suppression list
          • this address has never been emailed before (cross-lead dedup)
          • we're under LEADS_AUTOSEND_DAILY_CAP for the rolling 24h
        """
        cap = email_out.daily_cap()
        sent_today = store.autosent_today()
        budget = max(0, cap - sent_today)
        if budget <= 0:
            log.info("Autosend skipped — daily cap reached (%d/%d)", sent_today, cap)
            return

        candidates = store.autosend_candidates(min_score=email_out.min_score(), limit=budget * 2)
        if not candidates:
            return

        sent = 0
        for lead in candidates:
            if sent >= budget:
                break
            # Final per-recipient safety check (race-safe).
            if store.is_suppressed(lead["email"]):
                continue
            topic = topic_by_key(lead["dashboard_key"])
            if topic is None:
                continue
            ok = email_out.send(
                to_email=lead["email"],
                ref_code=lead["ref_code"] or "",
                source=lead["source"],
                topic_display=topic.key.replace("_", " "),
                body_text=lead["draft"] or "",
            )
            if ok:
                store.mark_auto_sent(lead["id"])
                sent += 1
                # Polite spacing between sends — 5s gap is well under any
                # reasonable SMTP rate limit and avoids burst-flagging by
                # downstream filters.
                await asyncio.sleep(5.0)
        log.info("Autosend pass complete — sent %d/%d (cap %d, used %d/%d today)",
                 sent, len(candidates), cap, sent_today + sent, cap)
