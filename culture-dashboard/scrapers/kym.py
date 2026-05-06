"""Know Your Meme — newest entries.

Pulls KYM's RSS feed of newly-confirmed meme entries. RSS is stable and
needs no key.
"""

from __future__ import annotations

import logging
import time
from email.utils import parsedate_to_datetime

import feedparser

from models import Item
from ._http import client

NAME = "kym_newest"
SECTION = "memes"
REFRESH_SECONDS = 6 * 60 * 60

log = logging.getLogger(__name__)

FEED = "https://knowyourmeme.com/newsfeed.rss"


async def fetch() -> list[Item]:
    async with client() as c:
        r = await c.get(FEED)
        r.raise_for_status()
        body = r.text
    parsed = feedparser.parse(body)
    now = time.time()
    items: list[Item] = []
    for e in parsed.entries[:40]:
        try:
            ts = parsedate_to_datetime(e.published).timestamp() if getattr(e, "published", None) else now
        except Exception:  # noqa: BLE001
            ts = now
        # Newer entries score higher.
        recency = max(0.0, 1.0 - (now - ts) / (7 * 86400))
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=e.get("title") or "(untitled)",
            url=e.get("link"),
            summary=(e.get("summary") or "")[:300],
            score=recency * 1000,
            extra={"published": e.get("published")},
        ))
    return items
