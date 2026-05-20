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
                fav = _favorite_market(ev)
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
                           "favorite_question": fav["question"],
                           "favorite_price": fav["price"],
                           "best_bid": fav["best_bid"],
                           "best_ask": fav["best_ask"],
                           "mid_price": fav["mid_price"],
                           "spread_bps": fav["spread_bps"]},
                ))
                if slug and fav["price"] is not None:
                    snapshots.append({
                        "event_slug": slug,
                        "ts": now,
                        "favorite_question": fav["question"] or "",
                        "favorite_price": fav["price"],
                        "volume": vol,
                        "best_bid": fav["best_bid"],
                        "best_ask": fav["best_ask"],
                        "mid_price": fav["mid_price"],
                        "spread_bps": fav["spread_bps"],
                    })
    return out


def _favorite_market(ev: dict) -> dict:
    """Pick the leading market within an event by `lastTradePrice`.

    Returns a dict with: question, price (lastTradePrice), best_bid, best_ask,
    mid_price ((bid+ask)/2), spread_bps. Any sub-field may be None when the
    market is thinly traded or the API omitted it.
    """
    markets = ev.get("markets") or []
    best_idx = -1
    best_price = -1.0
    for i, m in enumerate(markets):
        try:
            price = float(m.get("lastTradePrice") or 0)
        except (TypeError, ValueError):
            continue
        if price > best_price:
            best_price = price
            best_idx = i
    if best_idx == -1 and markets:
        # Fall back to outcomePrices[0] on the first market.
        m = markets[0]
        return {
            "question": m.get("question") or ev.get("title"),
            "price": _outcome_price_zero(m),
            "best_bid": None, "best_ask": None,
            "mid_price": None, "spread_bps": None,
        }
    if best_idx == -1:
        return {"question": None, "price": None, "best_bid": None,
                "best_ask": None, "mid_price": None, "spread_bps": None}

    m = markets[best_idx]
    bid = _to_float(m.get("bestBid"))
    ask = _to_float(m.get("bestAsk"))
    mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
    spread_bps = ((ask - bid) / mid * 10_000) if (mid and bid is not None and ask is not None and mid > 0) else None
    return {
        "question": m.get("question") or m.get("groupItemTitle") or ev.get("title"),
        "price": float(m.get("lastTradePrice") or 0),
        "best_bid": bid,
        "best_ask": ask,
        "mid_price": mid,
        "spread_bps": round(spread_bps, 1) if spread_bps is not None else None,
    }


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _outcome_price_zero(m: dict) -> float | None:
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            arr = json.loads(prices)
            if arr:
                return float(arr[0])
        except (json.JSONDecodeError, ValueError):
            return None
    return None
