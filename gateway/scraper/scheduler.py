"""
APScheduler running scrape jobs on configurable intervals.

Default schedule (all configurable via .env or admin panel API):
  Twitter:       every 20 minutes
  TruthSocial:   every 15 minutes

Each run:
  1. Load keywords from config/DB
  2. Run scraper for each keyword with rate limiting
  3. Store new posts in local SQLite
  4. Attempt transmission to main server
  5. Log run stats to ScraperRun table
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scraper.config import (
    TWITTER_INTERVAL_MINUTES,
    TRUTHSOCIAL_INTERVAL_MINUTES,
    RETRY_TRANSMISSION_INTERVAL_MINUTES,
)
from scraper.scrapers.twitter import TwitterScraper
from scraper.scrapers.truthsocial import TruthSocialScraper
from scraper.storage import db as store
from scraper.transmission.pusher import push_untransmitted

log = logging.getLogger("scraper")

scheduler = AsyncIOScheduler()
twitter_scraper = TwitterScraper()
truthsocial_scraper = TruthSocialScraper()


async def run_twitter_scrape() -> None:
    """Run a full Twitter scrape cycle."""
    try:
        from scraper.observability import tag_scraper_platform
        tag_scraper_platform("twitter")
    except Exception:
        pass
    keywords_map = store.get_keywords("twitter")
    keywords = keywords_map.get("twitter", [])
    if not keywords:
        log.info("Twitter: no keywords configured, skipping")
        return

    log.info("Twitter: starting scrape for %d keywords", len(keywords))
    start = time.monotonic()
    run_id = store.create_run("twitter", ",".join(keywords[:5]))

    total_found = 0
    total_new = 0
    error_msg = None

    try:
        posts = await twitter_scraper.fetch(keywords)
        total_found = len(posts)

        for post in posts:
            if store.insert_post(post):
                total_new += 1

        # Attempt transmission
        result = await push_untransmitted(platform="twitter")
        transmitted = result["pushed"]

    except Exception as e:
        error_msg = str(e)
        log.exception("Twitter scrape failed")
        transmitted = 0

    duration = time.monotonic() - start
    store.complete_run(run_id, total_found, total_new, transmitted, duration, error_msg)
    log.info("Twitter: done — found=%d new=%d transmitted=%d duration=%.1fs", total_found, total_new, transmitted, duration)


async def run_truthsocial_scrape() -> None:
    """Run a full TruthSocial scrape cycle."""
    try:
        from scraper.observability import tag_scraper_platform
        tag_scraper_platform("truthsocial")
    except Exception:
        pass
    keywords_map = store.get_keywords("truthsocial")
    keywords = keywords_map.get("truthsocial", [])

    log.info("TruthSocial: starting scrape for %d keywords + prominent accounts", len(keywords))
    start = time.monotonic()
    run_id = store.create_run("truthsocial", ",".join(keywords[:5]))

    total_found = 0
    total_new = 0
    error_msg = None

    try:
        posts = await truthsocial_scraper.fetch(keywords)
        total_found = len(posts)

        for post in posts:
            if store.insert_post(post):
                total_new += 1

        result = await push_untransmitted(platform="truthsocial")
        transmitted = result["pushed"]

    except Exception as e:
        error_msg = str(e)
        log.exception("TruthSocial scrape failed")
        transmitted = 0

    duration = time.monotonic() - start
    store.complete_run(run_id, total_found, total_new, transmitted, duration, error_msg)
    log.info("TruthSocial: done — found=%d new=%d transmitted=%d duration=%.1fs", total_found, total_new, transmitted, duration)


async def retry_transmission() -> None:
    """Retry pushing untransmitted posts to the main server."""
    result = await push_untransmitted()
    if result["pushed"] > 0 or result["failed"] > 0:
        log.info("Transmission retry: pushed=%d failed=%d skipped=%d", result["pushed"], result["failed"], result["skipped"])


def start_scheduler() -> None:
    """Configure and start the APScheduler."""
    scheduler.add_job(
        run_twitter_scrape,
        IntervalTrigger(minutes=TWITTER_INTERVAL_MINUTES),
        id="twitter_scrape",
        name="Twitter Scrape",
        replace_existing=True,
    )
    scheduler.add_job(
        run_truthsocial_scrape,
        IntervalTrigger(minutes=TRUTHSOCIAL_INTERVAL_MINUTES),
        id="truthsocial_scrape",
        name="TruthSocial Scrape",
        replace_existing=True,
    )
    scheduler.add_job(
        retry_transmission,
        IntervalTrigger(minutes=RETRY_TRANSMISSION_INTERVAL_MINUTES),
        id="retry_transmission",
        name="Retry Transmission",
        replace_existing=True,
    )
    scheduler.start()
    log.info(
        "Scheduler started — twitter=%dmin, truthsocial=%dmin, retry=%dmin",
        TWITTER_INTERVAL_MINUTES, TRUTHSOCIAL_INTERVAL_MINUTES, RETRY_TRANSMISSION_INTERVAL_MINUTES,
    )


def get_scheduler_status() -> dict:
    """Return status of all scheduled jobs."""
    jobs = []
    for job in scheduler.get_jobs():
        trigger = job.trigger
        interval_minutes = None
        if hasattr(trigger, "interval"):
            interval_minutes = int(trigger.interval.total_seconds() / 60)

        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "interval_minutes": interval_minutes,
            "paused": job.next_run_time is None,
        })
    return {
        "running": scheduler.running,
        "jobs": jobs,
    }


def pause_job(job_id: str) -> bool:
    try:
        scheduler.pause_job(job_id)
        return True
    except Exception:
        return False


def resume_job(job_id: str) -> bool:
    try:
        scheduler.resume_job(job_id)
        return True
    except Exception:
        return False


def trigger_job(job_id: str) -> bool:
    """Immediately trigger a scheduled job (runs in addition to schedule)."""
    job = scheduler.get_job(job_id)
    if not job:
        return False
    # Run the job function directly as a one-off
    asyncio.ensure_future(job.func())
    return True


def update_job_interval(job_id: str, interval_minutes: int) -> bool:
    try:
        scheduler.reschedule_job(
            job_id,
            trigger=IntervalTrigger(minutes=interval_minutes),
        )
        return True
    except Exception:
        return False
