"""Kalshi disaster-market fetcher (read-only, public API, no auth).

Pulls open disaster-related events + their nested markets from Kalshi's
``/trade-api/v2/events`` endpoint, normalises each market into a row with
title / threshold / YES price / volume / open interest / deep-link URL.

Why a separate Kalshi client from ``centralbank-dashboard/kalshi_client.py``:
  - That one is fine-tuned for FOMC bucket classification (cut25 / hold /
    hike25 / …); disasters need a count-threshold extraction.
  - We pull a different set of series tickers (HURRICANE / WILDFIRE /
    TORNADO / EARTHQUAKE / VOLCANO / ATMOSPHERIC).
  - Trade-out URL prefers Kalshi's *event* page so a user lands on the
    full ladder of count thresholds, not a single sub-market.

Authentication — the public ``/events`` and ``/markets`` endpoints don't
need auth. Order placement (Phase 2) would need RSA-PSS signing with each
user's own API key + private key; out of scope for this v0.4 read-only build.

Cache: 5 min — Kalshi prices move continuously and we don't want to hammer
their rate limit on every page load.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from . import _cache
from ._http import get as http_get

log = logging.getLogger("disasters.kalshi")

KALSHI_HOST = "https://api.elections.kalshi.com"
KALSHI_API = "/trade-api/v2"

# Series tickers we'll try. Kalshi's taxonomy shifts; this list is
# conservative + defensive (extra tickers just return empty results).
# Updated against the Kalshi catalog as of 2026-04.
SERIES_TICKERS: tuple[str, ...] = (
    "KXHURRICANE",          # tropical-cyclone landfall / category markets
    "KXATLHURR",            # Atlantic-basin season count
    "KXATLNAMED",           # Atlantic named storms
    "KXMAJORHURR",          # Atlantic major (Cat 3+) hurricanes
    "KXMAXWIND",            # max sustained wind
    "KXWILDFIRE",           # wildfire acres / count
    "KXCAFIRE",             # California-specific fires
    "KXEARTHQUAKE",         # global M-threshold quakes
    "KXBIGQUAKE",           # significant-quake markets
    "KXTORNADO",            # US tornado count
    "KXVOLCANO",            # volcano eruption markets
    "KXFEMA",               # FEMA major-disaster declarations
    "KXFLOOD",              # flood markets
    "KXTSUNAMI",            # tsunami markets
)

# Reject any series that's clearly off-topic but matched a wildcard
REJECT_KEYWORDS = (
    "stock", "ipo", "crypto", "bitcoin", "tesla", "spacex", "election",
    "president", "senate", "governor", "championship", "ufc", "nfl", "nba",
)

DISASTER_KEYWORDS = (
    "hurricane", "tropical storm", "named storm", "tropical cyclone", "typhoon",
    "earthquake", "magnitude", "tsunami", "tornado", "twister",
    "wildfire", "wild fire", "bushfire", "forest fire", "acres burned",
    "flood", "flooding", "category 5", "category 4", "category 3",
    "volcano", "volcanic", "eruption", "landslide", "mudslide",
    "fema", "disaster declaration", "evacuation", "natural disaster",
    "storm surge", "atmospheric river",
)


def _normalize_price(p) -> Optional[float]:
    """Kalshi v2 prices arrive either as 0-1 dollar floats or 1-99 cents.
    Normalise to a 0-1 probability."""
    if p is None:
        return None
    try:
        v = float(p)
    except (TypeError, ValueError):
        return None
    if v > 1.0:
        v = v / 100.0
    if v < 0 or v > 1:
        return None
    return round(v, 4)


def _yes_price(market: dict) -> Optional[float]:
    last = _normalize_price(market.get("last_price"))
    if last is not None and last > 0:
        return last
    bid = _normalize_price(market.get("yes_bid"))
    ask = _normalize_price(market.get("yes_ask"))
    if bid is not None and ask is not None and ask > 0:
        return round((bid + ask) / 2, 4)
    return bid


def _event_url(event_ticker: str, ticker: str) -> Optional[str]:
    if event_ticker:
        return f"https://kalshi.com/events/{event_ticker.lower()}"
    if ticker:
        return f"https://kalshi.com/markets/{ticker.lower()}"
    return None


def _fetch_events_for_series(series_ticker: str, status: str = "open") -> list[dict]:
    r = http_get(
        f"{KALSHI_HOST}{KALSHI_API}/events",
        params={
            "series_ticker": series_ticker,
            "status": status,
            "with_nested_markets": "true",
            "limit": "50",
        },
        timeout=15,
    )
    if not r:
        return []
    try:
        payload = r.json()
    except ValueError:
        return []
    return payload.get("events") or []


_RE_INTEGER_THRESHOLD = re.compile(r"\b(?:at\s+least|>=|over|more\s+than)?\s*(\d{1,4})\b", re.I)


def _topic_hint(title: str) -> str:
    tl = title.lower()
    if any(k in tl for k in ("hurricane", "named storm", "tropical")):
        return "hurricane"
    if "tornado" in tl:
        return "tornado"
    if any(k in tl for k in ("earthquake", "magnitude")):
        return "earthquake"
    if any(k in tl for k in ("wildfire", "wild fire", "forest fire", "bushfire", "acres burned")):
        return "wildfire"
    if any(k in tl for k in ("volcano", "volcanic", "eruption")):
        return "volcano"
    if "tsunami" in tl:
        return "tsunami"
    if "flood" in tl:
        return "flood"
    if "fema" in tl or "disaster declaration" in tl:
        return "fema"
    return "other"


def fetch_disaster_markets() -> dict:
    hit = _cache.get("kalshi_disasters", ttl_s=300)
    if hit is not None:
        return hit

    all_events: list[dict] = []
    for series in SERIES_TICKERS:
        chunk = _fetch_events_for_series(series)
        if chunk:
            log.info("Kalshi: %d events under series_ticker=%s", len(chunk), series)
            all_events.extend(chunk)

    if not all_events:
        out = {
            "source": "Kalshi /trade-api/v2/events (public)",
            "count": 0,
            "markets": [],
            "by_topic": {},
            "note": "No matching series tickers returned data (Kalshi catalog may have moved).",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        _cache.put("kalshi_disasters", out)
        return out

    rows: list[dict] = []
    seen: set[str] = set()
    for ev in all_events:
        ev_title = (ev.get("title") or "") + " " + (ev.get("sub_title") or "")
        ev_tl = ev_title.lower()
        if any(k in ev_tl for k in REJECT_KEYWORDS):
            continue
        ev_ticker = ev.get("event_ticker") or ev.get("ticker") or ""
        for m in (ev.get("markets") or []):
            if m.get("status") not in (None, "open", "active", "initialized"):
                continue
            title = m.get("title") or m.get("yes_sub_title") or ""
            subtitle = m.get("subtitle") or m.get("yes_sub_title") or ""
            text = f"{title} {subtitle}".strip()
            combined = f"{ev_title} {text}".lower()
            if not any(k in combined for k in DISASTER_KEYWORDS):
                continue
            ticker = m.get("ticker") or ""
            if ticker in seen:
                continue
            seen.add(ticker)
            price = _yes_price(m)
            topic = _topic_hint(f"{ev_title} {text}")
            # Try to extract an integer threshold from the title (e.g. "≥14")
            threshold: Optional[int] = None
            m_thr = _RE_INTEGER_THRESHOLD.search(text)
            if m_thr:
                try:
                    threshold = int(m_thr.group(1))
                except ValueError:
                    threshold = None
            rows.append({
                "title": text,
                "event_title": ev_title.strip(),
                "topic": topic,
                "threshold": threshold,
                "yes_price": price,
                "yes_bid": _normalize_price(m.get("yes_bid")),
                "yes_ask": _normalize_price(m.get("yes_ask")),
                "volume_24h": float(m.get("volume_24h") or 0),
                "open_interest": int(m.get("open_interest") or 0),
                "ticker": ticker,
                "event_ticker": ev_ticker,
                "close_time": m.get("close_time"),
                "url": _event_url(ev_ticker, ticker),
            })

    rows.sort(key=lambda r: r["volume_24h"], reverse=True)
    by_topic: dict[str, int] = {}
    for r in rows:
        by_topic[r["topic"]] = by_topic.get(r["topic"], 0) + 1

    out = {
        "source": "Kalshi /trade-api/v2/events (public)",
        "count": len(rows),
        "markets": rows,
        "by_topic": by_topic,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache.put("kalshi_disasters", out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_disaster_markets(), indent=2)[:2500])
