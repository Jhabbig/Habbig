"""Polymarket + Kalshi culture-bucket markets.

Polymarket has a public Gamma API (no key). Kalshi requires login for full
data; we use the public events listing where available.
"""

from __future__ import annotations

import logging

from models import Item
from ._http import client

NAME = "culture_markets"
SECTION = "markets"
REFRESH_SECONDS = 30 * 60

log = logging.getLogger(__name__)

# Polymarket "tag slugs" we treat as cultural.
POLY_TAGS = ["pop-culture", "entertainment", "music", "movies", "tv", "celebrity",
             "awards", "video-games", "internet"]


async def fetch() -> list[Item]:
    items: list[Item] = []
    items.extend(await _polymarket())
    return sorted(items, key=lambda i: i.score, reverse=True)[:60]


async def _polymarket() -> list[Item]:
    base = "https://gamma-api.polymarket.com/events"
    out: list[Item] = []
    async with client() as c:
        for tag in POLY_TAGS:
            try:
                r = await c.get(base, params={
                    "tag_slug": tag, "active": "true", "closed": "false",
                    "limit": 20, "order": "volume24hr", "ascending": "false",
                })
                r.raise_for_status()
                events = r.json() or []
            except Exception as e:  # noqa: BLE001
                log.warning("polymarket %s failed: %s", tag, e)
                continue
            for ev in events:
                vol = float(ev.get("volume24hr") or ev.get("volume") or 0)
                if vol < 1000:
                    continue
                out.append(Item(
                    section=SECTION,
                    source=NAME,
                    title=ev.get("title") or "(untitled)",
                    url=f"https://polymarket.com/event/{ev.get('slug', '')}",
                    image=ev.get("image"),
                    summary=(ev.get("description") or "")[:300],
                    score=vol,
                    extra={"venue": "polymarket", "tag": tag,
                           "liquidity": ev.get("liquidity")},
                ))
    return out
