"""Instagram trending Reels / hashtags.

Same backend hierarchy as TikTok:

  1. Apify  — `apify/instagram-scraper`
  2. RapidAPI Instagram scrapers
  3. Unofficial `instaloader`/`instagrapi` (free, accounts get banned)
  4. Disabled.

Instagram Graph API is intentionally not used — it only covers business/
creator accounts the user owns, which doesn't help for tracking memes.
"""

from __future__ import annotations

import logging
import os

from models import Item
from ._http import client

NAME = "instagram_trending"
SECTION = "memes"
REFRESH_SECONDS = 60 * 60

log = logging.getLogger(__name__)

# Hashtags whose top posts give a reasonable read on what's viral right now.
DEFAULT_TAGS = ["meme", "reels", "viral", "fyp", "trending"]


async def fetch() -> list[Item]:
    tags = (os.environ.get("INSTAGRAM_TAGS") or ",".join(DEFAULT_TAGS)).split(",")
    tags = [t.strip().lstrip("#") for t in tags if t.strip()]

    if os.environ.get("APIFY_TOKEN"):
        return await _via_apify(tags)
    if os.environ.get("RAPIDAPI_KEY"):
        return await _via_rapidapi(tags)
    if os.environ.get("INSTAGRAM_USERNAME") and os.environ.get("INSTAGRAM_PASSWORD"):
        return await _via_unofficial(tags)
    log.info("instagram: no backend configured")
    return []


async def _via_apify(tags: list[str]) -> list[Item]:
    token = os.environ["APIFY_TOKEN"]
    actor = os.environ.get("APIFY_INSTAGRAM_ACTOR", "apify~instagram-scraper")
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}"
    payload = {
        "search": [f"#{t}" for t in tags],
        "searchType": "hashtag",
        "resultsType": "posts",
        "resultsLimit": 30,
    }
    async with client() as c:
        r = await c.post(url, json=payload)
        r.raise_for_status()
        rows = r.json() or []
    items = []
    for row in rows[:60]:
        likes = int(row.get("likesCount") or 0)
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=(row.get("caption") or "(no caption)")[:200],
            url=row.get("url"),
            image=row.get("displayUrl"),
            score=float(likes),
            velocity=float(row.get("commentsCount") or 0),
            extra={"hashtag": row.get("hashtag"),
                   "owner": row.get("ownerUsername")},
        ))
    return items


async def _via_rapidapi(tags: list[str]) -> list[Item]:
    key = os.environ["RAPIDAPI_KEY"]
    host = os.environ.get("RAPIDAPI_INSTAGRAM_HOST", "instagram-scraper-api2.p.rapidapi.com")
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": host}
    items: list[Item] = []
    async with client(headers=headers) as c:
        for tag in tags[:5]:
            try:
                r = await c.get(f"https://{host}/v1/hashtag", params={"hashtag": tag})
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001
                log.warning("ig hashtag %s failed: %s", tag, e)
                continue
            data = r.json() or {}
            for p in ((data.get("data") or {}).get("items") or [])[:20]:
                stats = p.get("like_count") or 0
                items.append(Item(
                    section=SECTION,
                    source=NAME,
                    title=((p.get("caption") or {}).get("text") or "(no caption)")[:200],
                    url=f"https://www.instagram.com/p/{p.get('code', '')}/" if p.get("code") else None,
                    image=(p.get("image_versions2", {}).get("candidates") or [{}])[0].get("url"),
                    score=float(stats),
                    extra={"hashtag": tag},
                ))
    return items


async def _via_unofficial(tags: list[str]) -> list[Item]:
    try:
        import instaloader  # type: ignore
    except ImportError:
        log.info("instagram: instaloader not installed; skipping unofficial path")
        return []
    L = instaloader.Instaloader(quiet=True)
    try:
        L.login(os.environ["INSTAGRAM_USERNAME"], os.environ["INSTAGRAM_PASSWORD"])
    except Exception as e:  # noqa: BLE001
        log.warning("instagram unofficial login failed: %s", e)
        return []
    items: list[Item] = []
    for tag in tags[:3]:
        try:
            ht = instaloader.Hashtag.from_name(L.context, tag)
            for i, post in enumerate(ht.get_top_posts()):
                if i >= 10:
                    break
                items.append(Item(
                    section=SECTION,
                    source=NAME,
                    title=(post.caption or "(no caption)")[:200],
                    url=f"https://www.instagram.com/p/{post.shortcode}/",
                    image=post.url,
                    score=float(post.likes),
                    velocity=float(post.comments),
                    extra={"hashtag": tag, "owner": post.owner_username},
                ))
        except Exception as e:  # noqa: BLE001
            log.warning("ig hashtag %s failed: %s", tag, e)
    return items
