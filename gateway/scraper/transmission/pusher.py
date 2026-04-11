"""
Pushes scraped posts to the main server.

Authentication:
  - All requests include: Authorization: Bearer {SCRAPER_API_KEY}
  - Main server validates this key on the /api/scraper/* endpoints

Push endpoint on main server: POST /api/scraper/ingest
Payload: {posts: [...], scraper_run_id: int, platform: str}

Retry logic:
  - On failure: exponential backoff (2s, 4s, 8s, max 60s)
  - After MAX_TRANSMISSION_ATTEMPTS: mark as permanently failed
  - Network errors: retry
  - 401: log critical error, stop retrying (key mismatch)
  - 429: back off 120 seconds
  - 5xx: retry with backoff
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from scraper.config import MAIN_SERVER_URL, SCRAPER_API_KEY, MAX_TRANSMISSION_ATTEMPTS
from scraper.storage import db as store
from scraper.storage.models import RawPost

log = logging.getLogger("scraper")

BATCH_SIZE = 50


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SCRAPER_API_KEY}"}


async def push_untransmitted(platform: str | None = None) -> dict:
    """
    Push all posts where transmitted=False to main server.
    Returns summary: {pushed: int, failed: int, skipped: int}
    """
    posts = store.get_untransmitted(platform=platform, limit=500)
    if not posts:
        return {"pushed": 0, "failed": 0, "skipped": 0}

    pushed = 0
    failed = 0
    skipped = 0

    # Process in batches
    for i in range(0, len(posts), BATCH_SIZE):
        batch = posts[i:i + BATCH_SIZE]

        # Skip posts that have exceeded max attempts
        to_send = []
        for p in batch:
            if p.transmission_attempts >= MAX_TRANSMISSION_ATTEMPTS:
                skipped += 1
                continue
            to_send.append(p)

        if not to_send:
            continue

        ok = await push_batch(to_send)
        if ok:
            store.mark_transmitted([p.id for p in to_send])
            pushed += len(to_send)
        else:
            store.increment_transmission_attempts([p.id for p in to_send])
            failed += len(to_send)

    return {"pushed": pushed, "failed": failed, "skipped": skipped}


async def push_batch(posts: list[RawPost], run_id: int | None = None) -> bool:
    """Push a single batch to the main server. Returns True if successful."""
    if not SCRAPER_API_KEY:
        log.error("SCRAPER_API_KEY not set — cannot push to main server")
        return False

    url = f"{MAIN_SERVER_URL}/api/scraper/ingest"
    payload = {
        "posts": [p.to_dict() for p in posts],
        "scraper_run_id": run_id,
        "platform": posts[0].platform if posts else "unknown",
    }

    max_retries = 3
    backoff = 2

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload, headers=_auth_headers())

                if resp.status_code == 200:
                    log.info("Pushed %d posts to main server", len(posts))
                    return True
                elif resp.status_code == 401:
                    log.critical("401 from main server — SCRAPER_API_KEY mismatch! Stopping retries.")
                    return False
                elif resp.status_code == 429:
                    log.warning("429 from main server — rate limited, backing off 120s")
                    await asyncio.sleep(120)
                    continue
                elif resp.status_code >= 500:
                    log.warning("Server error %d from main server, retry %d/%d", resp.status_code, attempt + 1, max_retries)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                else:
                    log.error("Unexpected status %d from main server: %s", resp.status_code, resp.text[:200])
                    return False

        except httpx.TimeoutException:
            log.warning("Timeout pushing to main server, retry %d/%d", attempt + 1, max_retries)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except httpx.ConnectError:
            log.warning("Cannot connect to main server at %s, retry %d/%d", MAIN_SERVER_URL, attempt + 1, max_retries)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception:
            log.exception("Unexpected error pushing to main server")
            return False

    log.error("All retries exhausted pushing batch of %d posts", len(posts))
    return False
