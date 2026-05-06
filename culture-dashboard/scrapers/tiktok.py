"""TikTok trending hashtags + sounds.

Backend hierarchy, picked at call time:

  1. Apify  — actor `clockworks/tiktok-scraper` (most reliable)
  2. RapidAPI ScrapTik or TikAPI
  3. Unofficial `TikTokApi` library (free, fragile)
  4. Disabled — returns empty list, dashboard degrades gracefully.

ToS note: TikTok's ToS prohibit scraping. Use a paid backend for any
production use. The unofficial path exists for dev and is best-effort.
"""

from __future__ import annotations

import logging
import os

from models import Item
from ._http import client

NAME = "tiktok_trending"
SECTION = "memes"
REFRESH_SECONDS = 60 * 60        # hourly

log = logging.getLogger(__name__)


async def fetch() -> list[Item]:
    if os.environ.get("APIFY_TOKEN"):
        return await _via_apify()
    if os.environ.get("RAPIDAPI_KEY"):
        return await _via_rapidapi()
    if os.environ.get("TIKTOK_MS_TOKEN"):
        return await _via_unofficial()
    log.info("tiktok: no backend configured (APIFY_TOKEN / RAPIDAPI_KEY / TIKTOK_MS_TOKEN)")
    return []


async def _via_apify() -> list[Item]:
    token = os.environ["APIFY_TOKEN"]
    actor = os.environ.get("APIFY_TIKTOK_ACTOR", "clockworks~tiktok-scraper")
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}"
    payload = {"hashtags": ["fyp", "viral"], "resultsPerPage": 30}
    async with client() as c:
        r = await c.post(url, json=payload)
        r.raise_for_status()
        rows = r.json() or []
    return [_normalize_apify(row) for row in rows][:50]


def _normalize_apify(row: dict) -> Item:
    plays = int(row.get("playCount") or 0)
    return Item(
        section=SECTION,
        source=NAME,
        title=row.get("text") or row.get("hashtagName") or "(untitled)",
        url=row.get("webVideoUrl") or row.get("videoUrl"),
        image=row.get("videoMeta", {}).get("coverUrl") if isinstance(row.get("videoMeta"), dict) else None,
        score=float(plays),
        velocity=float(row.get("diggCount") or 0),
        extra={"author": row.get("authorMeta", {}).get("name") if isinstance(row.get("authorMeta"), dict) else None,
               "hashtags": [h.get("name") for h in (row.get("hashtags") or []) if isinstance(h, dict)]},
    )


async def _via_rapidapi() -> list[Item]:
    key = os.environ["RAPIDAPI_KEY"]
    host = os.environ.get("RAPIDAPI_TIKTOK_HOST", "scraptik.p.rapidapi.com")
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": host}
    async with client(headers=headers) as c:
        r = await c.get(f"https://{host}/trending/feed", params={"region": "US"})
        r.raise_for_status()
        data = r.json() or {}
    items = []
    for v in (data.get("aweme_list") or [])[:50]:
        stats = v.get("statistics") or {}
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=(v.get("desc") or "(untitled)")[:200],
            url=f"https://www.tiktok.com/@{v.get('author', {}).get('unique_id', '')}/video/{v.get('aweme_id', '')}",
            image=(v.get("video", {}).get("cover", {}).get("url_list") or [None])[0],
            score=float(stats.get("play_count") or 0),
            velocity=float(stats.get("digg_count") or 0),
            extra={"author": v.get("author", {}).get("unique_id")},
        ))
    return items


async def _via_unofficial() -> list[Item]:
    """Best-effort path using the `TikTokApi` library if installed."""
    try:
        from TikTokApi import TikTokApi  # type: ignore
    except ImportError:
        log.info("tiktok: TikTokApi not installed; skipping unofficial path")
        return []
    ms_token = os.environ.get("TIKTOK_MS_TOKEN")
    items: list[Item] = []
    async with TikTokApi() as api:
        await api.create_sessions(ms_tokens=[ms_token], num_sessions=1, sleep_after=3)
        async for video in api.trending.videos(count=30):
            d = video.as_dict
            stats = d.get("stats") or {}
            items.append(Item(
                section=SECTION,
                source=NAME,
                title=(d.get("desc") or "(untitled)")[:200],
                url=f"https://www.tiktok.com/@{d.get('author', {}).get('uniqueId', '')}/video/{d.get('id', '')}",
                score=float(stats.get("playCount") or 0),
                velocity=float(stats.get("diggCount") or 0),
                extra={"author": d.get("author", {}).get("uniqueId")},
            ))
    return items
