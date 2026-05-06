from __future__ import annotations
"""Shared HTTP retry helper for aggregators.

Wraps an aiohttp GET with exponential backoff + jitter. Retries on 429,
5xx, and transport errors (timeouts, connection drops). Returns the
parsed JSON body on success, or ``None`` after all attempts fail.

The function deliberately swallows exceptions and logs them — the data
refresh loop is best-effort and a missing aggregator should not crash
sibling sources.
"""

import asyncio
import logging
import random
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


async def fetch_json_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict | None = None,
    timeout: float = 15.0,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 32.0,
    source_label: str = "aggregator",
) -> Optional[Any]:
    """GET *url* and return parsed JSON, retrying on 429/5xx/transport errors.

    Backoff: ``base_delay * 2**attempt`` capped at ``max_delay``, plus 0–1s jitter.
    Returns ``None`` if every attempt fails.
    """
    for attempt in range(max_attempts):
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 429 or 500 <= resp.status < 600:
                    if attempt + 1 < max_attempts:
                        delay = min(base_delay * (2 ** attempt), max_delay) + random.random()
                        logger.warning(
                            "%s %s status=%s — retry %d/%d in %.1fs",
                            source_label, url, resp.status, attempt + 1, max_attempts, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                logger.error("%s %s status=%s — giving up", source_label, url, resp.status)
                return None
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            if attempt + 1 < max_attempts:
                delay = min(base_delay * (2 ** attempt), max_delay) + random.random()
                logger.warning(
                    "%s %s error=%s — retry %d/%d in %.1fs",
                    source_label, url, type(e).__name__, attempt + 1, max_attempts, delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.error("%s %s exhausted retries: %s", source_label, url, e)
            return None
    return None


async def fetch_text_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict | None = None,
    timeout: float = 30.0,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 32.0,
    source_label: str = "aggregator",
) -> Optional[str]:
    """GET *url* and return response text, retrying on 429/5xx/transport errors."""
    for attempt in range(max_attempts):
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status == 200:
                    return await resp.text()
                if resp.status == 429 or 500 <= resp.status < 600:
                    if attempt + 1 < max_attempts:
                        delay = min(base_delay * (2 ** attempt), max_delay) + random.random()
                        logger.warning(
                            "%s %s status=%s — retry %d/%d in %.1fs",
                            source_label, url, resp.status, attempt + 1, max_attempts, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                logger.error("%s %s status=%s — giving up", source_label, url, resp.status)
                return None
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            if attempt + 1 < max_attempts:
                delay = min(base_delay * (2 ** attempt), max_delay) + random.random()
                logger.warning(
                    "%s %s error=%s — retry %d/%d in %.1fs",
                    source_label, url, type(e).__name__, attempt + 1, max_attempts, delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.error("%s %s exhausted retries: %s", source_label, url, e)
            return None
    return None
