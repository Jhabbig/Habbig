"""YouTube #trending via the official Data API."""

from __future__ import annotations

import logging
import os

from models import Item
from ._http import client

NAME = "youtube_trending"
SECTION = "attention"
REFRESH_SECONDS = 60 * 60

log = logging.getLogger(__name__)


async def fetch() -> list[Item]:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        return []
    async with client() as c:
        r = await c.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "snippet,statistics",
                "chart": "mostPopular",
                "regionCode": os.environ.get("CULTURE_GEO", "US"),
                "maxResults": 30,
                "key": key,
            },
        )
        r.raise_for_status()
        data = r.json() or {}
    items = []
    for v in data.get("items") or []:
        s = v.get("snippet") or {}
        st = v.get("statistics") or {}
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=s.get("title") or "(untitled)",
            url=f"https://www.youtube.com/watch?v={v.get('id', '')}",
            image=(s.get("thumbnails") or {}).get("high", {}).get("url"),
            score=float(st.get("viewCount") or 0),
            velocity=float(st.get("likeCount") or 0),
            extra={"channel": s.get("channelTitle")},
        ))
    return items
