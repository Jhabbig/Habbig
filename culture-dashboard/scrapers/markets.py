"""Polymarket + Kalshi culture-bucket markets.

Polymarket has a public Gamma API (no key). Kalshi requires login for full
data; we use the public events listing where available.

On every sweep we also append a price snapshot per event (favorite market's
question + price + volume) into the `market_prices` table so velocity-based
edges can compare topic surge against market movement.
"""

from __future__ import annotations

import json
import logging
import time

import cache
from models import Item
from ._http import client

NAME = "culture_markets"
SECTION = "markets"
REFRESH_SECONDS = 30 * 60

log = logging.getLogger(__name__)

POLY_TAGS = ["pop-culture", "entertainment", "music", "movies", "tv", "celebrity",
             "awards", "video-games", "internet"]


async def fetch() -> list[Item]:
    items: list[Item] = []
    snapshots: list[dict] = []
    items.extend(await _polymarket(snapshots))
    if snapshots:
        cache.record_market_prices(snapshots)
    return sorted(items, key=lambda i: i.score, reverse=True)[:60]


async def _polymarket(snapshots: list[dict]) -> list[Item]:
    base = "https://gamma-api.polymarket.com/events"
    out: list[Item] = []
    now = time.time()
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
                slug = ev.get("slug") or ""
                fav_q, fav_price = _favorite_market(ev)
                out.append(Item(
                    section=SECTION,
                    source=NAME,
                    title=ev.get("title") or "(untitled)",
                    url=f"https://polymarket.com/event/{slug}",
                    image=ev.get("image"),
                    summary=(ev.get("description") or "")[:300],
                    score=vol,
                    extra={"venue": "polymarket", "tag": tag,
                           "liquidity": ev.get("liquidity"),
                           "event_slug": slug,
                           "favorite_question": fav_q,
                           "favorite_price": fav_price},
                ))
                if slug and fav_price is not None:
                    snapshots.append({
                        "event_slug": slug,
                        "ts": now,
                        "favorite_question": fav_q or "",
                        "favorite_price": fav_price,
                        "volume": vol,
                    })
    return out


def _favorite_market(ev: dict) -> tuple[str | None, float | None]:
    """Pick the leading market within an event by `lastTradePrice`.

    Single-market events: returns market[0] (typically yes/no, "Yes" leg).
    Multi-market events: returns the candidate with highest lastTradePrice.
    """
    markets = ev.get("markets") or []
    best_q: str | None = None
    best_price: float | None = None
    for m in markets:
        try:
            price = float(m.get("lastTradePrice") or 0)
        except (TypeError, ValueError):
            continue
        if best_price is None or price > best_price:
            best_price = price
            best_q = m.get("question") or m.get("groupItemTitle") or ev.get("title")
    if best_price is None and markets:
        # No lastTradePrice — try outcomePrices[0] as a fallback.
        m = markets[0]
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            try:
                arr = json.loads(prices)
                if arr:
                    best_price = float(arr[0])
                    best_q = m.get("question") or ev.get("title")
            except (json.JSONDecodeError, ValueError):
                pass
    return best_q, best_price
