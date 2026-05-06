"""Apple Music top songs (RSS / Marketing API — public, no key)."""

from __future__ import annotations

import logging

from models import Item
from ._http import client

NAME = "apple_music_top"
SECTION = "entertainment"
REFRESH_SECONDS = 6 * 60 * 60

log = logging.getLogger(__name__)

URL = ("https://rss.applemarketingtools.com/api/v2/us/music/most-played/50/songs.json")


async def fetch() -> list[Item]:
    async with client() as c:
        r = await c.get(URL)
        r.raise_for_status()
        data = r.json() or {}
    feed = (data.get("feed") or {}).get("results") or []
    items = []
    for i, t in enumerate(feed[:50]):
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=f"{t.get('name')} — {t.get('artistName')}",
            url=t.get("url"),
            image=t.get("artworkUrl100"),
            score=float(50 - i),     # rank-based score, #1 = 50
            extra={"rank": i + 1, "artist": t.get("artistName")},
        ))
    return items
