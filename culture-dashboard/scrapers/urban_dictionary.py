"""Urban Dictionary — words of the day."""

from __future__ import annotations

import logging

from models import Item
from ._http import client

NAME = "urban_dictionary_wotd"
SECTION = "language"
REFRESH_SECONDS = 24 * 60 * 60

log = logging.getLogger(__name__)


async def fetch() -> list[Item]:
    async with client() as c:
        r = await c.get("https://api.urbandictionary.com/v0/words_of_the_day")
        r.raise_for_status()
        data = r.json() or {}
    out = []
    for i, w in enumerate((data.get("list") or [])[:14]):
        out.append(Item(
            section=SECTION,
            source=NAME,
            title=w.get("word") or "(untitled)",
            url=f"https://www.urbandictionary.com/define.php?term={w.get('word', '')}",
            summary=(w.get("definition") or "")[:300],
            score=float((w.get("thumbs_up") or 0) - (w.get("thumbs_down") or 0)),
            velocity=float(w.get("thumbs_up") or 0),
            extra={"date": w.get("date"), "example": (w.get("example") or "")[:200]},
        ))
    return out
