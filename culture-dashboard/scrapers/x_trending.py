"""X / Twitter trending topics.

The free X API tier no longer exposes /trends, so this scraper requires
either:
  * X_BEARER_TOKEN with the Pro tier, or
  * APIFY_TOKEN with a trending-topics actor, or
  * RAPIDAPI_KEY with a trending-topics endpoint.

Returns empty if none are configured.
"""

from __future__ import annotations

import logging
import os

from models import Item
from ._http import client

NAME = "x_trending"
SECTION = "attention"
REFRESH_SECONDS = 60 * 60

log = logging.getLogger(__name__)


async def fetch() -> list[Item]:
    if os.environ.get("APIFY_TOKEN"):
        return await _via_apify()
    if os.environ.get("RAPIDAPI_KEY"):
        return await _via_rapidapi()
    return []


async def _via_apify() -> list[Item]:
    token = os.environ["APIFY_TOKEN"]
    actor = os.environ.get("APIFY_X_TRENDS_ACTOR", "epctex~twitter-trends-scraper")
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}"
    async with client() as c:
        r = await c.post(url, json={"countries": ["United States"]})
        r.raise_for_status()
        rows = r.json() or []
    items = []
    for row in rows[:50]:
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=row.get("name") or row.get("trend") or "(untitled)",
            url=row.get("url"),
            score=float(row.get("tweet_volume") or row.get("volume") or 0),
        ))
    return items


async def _via_rapidapi() -> list[Item]:
    key = os.environ["RAPIDAPI_KEY"]
    host = os.environ.get("RAPIDAPI_X_HOST", "twitter154.p.rapidapi.com")
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": host}
    async with client(headers=headers) as c:
        r = await c.get(f"https://{host}/trends/", params={"woeid": 23424977})  # USA
        r.raise_for_status()
        data = r.json() or {}
    items = []
    for t in (data.get("trends") or [])[:50]:
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=t.get("name") or "(untitled)",
            url=t.get("url"),
            score=float(t.get("tweet_volume") or 0),
        ))
    return items
