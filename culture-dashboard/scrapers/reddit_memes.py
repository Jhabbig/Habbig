"""Reddit meme & culture subs — top of the day.

No auth needed for public JSON listings. Reddit will rate-limit unauth'd
requests harder than auth'd ones; if you hit limits set REDDIT_CLIENT_ID +
REDDIT_CLIENT_SECRET and we'll switch to OAuth.
"""

from __future__ import annotations

import logging
import os

from models import Item
from ._http import client

NAME = "reddit_memes"
SECTION = "memes"
REFRESH_SECONDS = 30 * 60

log = logging.getLogger(__name__)

SUBS = ["memes", "dankmemes", "MemeEconomy", "PoliticalHumor",
        "OutOfTheLoop", "AdviceAnimals", "Tinder"]


async def fetch() -> list[Item]:
    items: list[Item] = []
    headers = {"User-Agent": os.environ.get("REDDIT_USER_AGENT", "culture-dashboard/0.1")}
    async with client(headers=headers) as c:
        for sub in SUBS:
            try:
                r = await c.get(f"https://www.reddit.com/r/{sub}/top.json",
                                params={"t": "day", "limit": 15})
                r.raise_for_status()
                data = r.json() or {}
            except Exception as e:  # noqa: BLE001
                log.warning("reddit %s failed: %s", sub, e)
                continue
            for child in (data.get("data", {}).get("children") or []):
                d = child.get("data") or {}
                if d.get("over_18"):
                    continue
                items.append(Item(
                    section=SECTION,
                    source=NAME,
                    title=d.get("title") or "(untitled)",
                    url=f"https://reddit.com{d.get('permalink', '')}",
                    image=_image(d),
                    summary=d.get("selftext") or None,
                    score=float(d.get("ups") or 0),
                    velocity=float(d.get("num_comments") or 0),
                    extra={"sub": sub, "author": d.get("author")},
                ))
    items.sort(key=lambda i: i.score, reverse=True)
    return items[:60]


def _image(d: dict) -> str | None:
    if d.get("thumbnail", "").startswith("http"):
        return d["thumbnail"]
    preview = d.get("preview", {}).get("images")
    if preview:
        src = preview[0].get("source", {}).get("url")
        if src:
            return src.replace("&amp;", "&")
    return None
