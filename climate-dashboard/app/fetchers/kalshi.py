"""Kalshi climate markets.

Public events endpoint at api.elections.kalshi.com. Climate-related markets
live across "Weather", "Climate", and miscellaneous tickers; we filter by
title keywords (same denylist+allowlist approach as Polymarket).

Kalshi prices are integer cents (0-100). We convert to 0-1 probabilities
so downstream scoring can treat Kalshi and Polymarket markets the same way.

URL is best-effort: if Kalshi restructures their public API, the integration
silently returns nothing (no Kalshi markets in /api/markets) — the
Polymarket markets and the rest of the dashboard keep working.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .. import cache, http

logger = logging.getLogger("climate.kalshi")

URL = "https://api.elections.kalshi.com/trade-api/v2/events?status=open&limit=200&with_nested_markets=true"

# Same denylist/allowlist as Polymarket — keep the two venues consistent
REJECT_KEYWORDS = (
    "nfl", "nba", "nhl", "mlb", "mls", "rugby", "premier league", "ligue 1",
    "champion", "playoff", "election", "president", "senate", "governor",
    "ipo", "stock", "bitcoin", "crypto", "tesla", "spacex", "starship",
    "head-to-head", "champions league", "fight", "boxing",
    # Kalshi has lots of these; they belong in other dashboards
    "hurricane", "tornado",
)

CLIMATE_KEYWORDS = (
    "warmest", "hottest year", "global temperature", "global average",
    "climate", "co2", "carbon dioxide", "ppm", "sea ice", "arctic",
    "antarctic", "sea level", "ipcc", "1.5", "2 degrees", "paris agreement",
    "el nino", "la nina", "enso", "ocean temperature", "sst",
)


def _to_probability(cents: object) -> Optional[float]:
    """Kalshi prices are integer cents; convert to (0,1)."""
    if cents is None:
        return None
    try:
        f = float(cents) / 100.0
    except (TypeError, ValueError):
        return None
    if 0 < f < 1:
        return f
    return None


def _normalize_market(m: dict, event_title: str, event_category: str) -> dict:
    """Map a Kalshi market record into the Polymarket-shaped dict that the
    rest of the dashboard consumes."""
    last = _to_probability(m.get("last_price"))
    bid = _to_probability(m.get("yes_bid"))
    ask = _to_probability(m.get("yes_ask"))
    # Implied prefers last; falls back to mid-of-bid-ask
    implied = last
    if implied is None and bid is not None and ask is not None:
        implied = (bid + ask) / 2.0
    return {
        "conditionId": m.get("ticker", ""),
        "id": m.get("ticker", ""),
        "slug": (m.get("ticker") or "").lower(),
        "question": m.get("subtitle") or m.get("yes_sub_title") or m.get("title") or "",
        "lastTradePrice": last,
        "bestBid": bid,
        "bestAsk": ask,
        "liquidity": float(m.get("open_interest") or 0),
        "endDate": m.get("expiration_time"),
        "_event_title": event_title,
        "_event_tags": [event_category] if event_category else [],
        "_venue": "kalshi",
    }


def fetch() -> list[dict]:
    cached = cache.get("kalshi")
    if cached is not None:
        return cached
    r = http.get(URL, timeout=20)
    if not r:
        return []
    try:
        data = r.json()
    except Exception:
        logger.warning("Kalshi: response was not JSON")
        return []
    events = data.get("events") or []
    out: list[dict] = []
    for event in events:
        title = event.get("title", "") or ""
        category = event.get("category", "") or ""
        tl = title.lower()
        if any(k in tl for k in REJECT_KEYWORDS):
            continue
        if not (any(k in tl for k in CLIMATE_KEYWORDS) or "climate" in category.lower()):
            continue
        for m in event.get("markets") or []:
            out.append(_normalize_market(m, title, category))
    logger.info("Kalshi: %d climate markets (from %d events)", len(out), len(events))
    cache.set("kalshi", out)
    return out
