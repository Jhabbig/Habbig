"""Substack rising — pulls the public Notes / Featured feed.

Substack doesn't expose a stable trending API. We fall back to the
top-by-section RSS for a few major sections that capture culture.
"""

from __future__ import annotations

import logging

import feedparser

from models import Item
from ._http import client

NAME = "substack_rising"
SECTION = "language"
REFRESH_SECONDS = 6 * 60 * 60

log = logging.getLogger(__name__)

# Section leaderboards rendered as RSS by Substack itself.
SECTIONS_FEEDS = [
    ("Culture", "https://substack.com/feed/leaderboard/culture/featured"),
    ("Politics", "https://substack.com/feed/leaderboard/politics/featured"),
    ("Tech", "https://substack.com/feed/leaderboard/tech/featured"),
]


async def fetch() -> list[Item]:
    items: list[Item] = []
    async with client() as c:
        for label, url in SECTIONS_FEEDS:
            try:
                r = await c.get(url)
                if r.status_code != 200:
                    continue
            except Exception as e:  # noqa: BLE001
                log.warning("substack %s failed: %s", label, e)
                continue
            parsed = feedparser.parse(r.text)
            for i, e in enumerate(parsed.entries[:10]):
                items.append(Item(
                    section=SECTION,
                    source=NAME,
                    title=e.get("title") or "(untitled)",
                    url=e.get("link"),
                    summary=(e.get("summary") or "")[:300],
                    score=float(10 - i),       # rank-based
                    extra={"section": label},
                ))
    return items
