"""Polymarket gamma-API fetcher for disaster prediction markets.

Pattern follows ``climate-dashboard/server.py:fetch_climate_markets`` - tag
slugs first, then a hard keyword-allow filter on event title + question,
then a reject filter for the unrelated topics that share keywords with
disasters (sports brackets, election "landslide", "earthquake" used
metaphorically, etc.).
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from . import _cache
from ._http import get as http_get

log = logging.getLogger("disasters.polymarket")

GAMMA_BASE = "https://gamma-api.polymarket.com"

DISASTER_TAG_SLUGS: tuple[str, ...] = (
    "extreme-weather", "natural-disaster", "weather", "hurricane",
    "earthquake", "wildfire", "tornado", "flood",
)

# Words that indicate the market really is about a natural disaster.
DISASTER_KEYWORDS: tuple[str, ...] = (
    "hurricane", "tropical storm", "named storm", "tropical cyclone", "typhoon",
    "earthquake", "magnitude", "richter", "tsunami", "tornado", "twister",
    "wildfire", "wild fire", "bushfire", "forest fire", "acres burned",
    "flood", "flooding", "hurricane category", "category 5", "category 4",
    "volcano", "volcanic", "eruption", "landslide", "mudslide",
    "fema", "disaster declaration", "evacuation", "natural disaster",
    "storm surge", "atmospheric river", "saffir",
)

# Reject markets that share keywords with disasters but are about something else.
REJECT_KEYWORDS: tuple[str, ...] = (
    "election landslide",   # "Will it be a landslide?"
    "stock market crash",
    "bitcoin crash", "crypto crash",
    "championship", "playoff", "premier league", "nba", "nfl", "nhl", "mlb",
    "ipo", "tesla", "spacex",
)


def _fetch_events_by_tag(tag_slug: str, seen_ids: set, all_markets: list,
                         lock: threading.Lock) -> None:
    offset = 0
    for _ in range(8):  # cap pagination at 800
        r = http_get(
            f"{GAMMA_BASE}/events",
            params={"tag_slug": tag_slug, "closed": "false",
                    "limit": "100", "offset": str(offset)},
        )
        if not r:
            break
        try:
            events = r.json()
        except ValueError:
            break
        if not events:
            break
        for event in events:
            title = (event.get("title", "") or "")
            tl = title.lower()
            if any(k in tl for k in REJECT_KEYWORDS):
                continue
            tags = event.get("tags", []) or []
            tag_labels = [t.get("label", "") for t in tags if isinstance(t, dict)]
            for m in event.get("markets", []) or []:
                mid = m.get("conditionId") or m.get("id", "")
                if not mid:
                    continue
                with lock:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    m["_event_title"] = title
                    m["_event_slug"] = event.get("slug")
                    m["_event_tags"] = tag_labels
                    all_markets.append(m)
        offset += 100


def fetch_disaster_markets() -> list[dict]:
    hit = _cache.get("polymarket_disasters", ttl_s=300)  # 5 min
    if hit is not None:
        return hit
    all_markets: list[dict] = []
    seen_ids: set = set()
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_fetch_events_by_tag, slug, seen_ids, all_markets, lock)
                   for slug in DISASTER_TAG_SLUGS]
        for f in futures:
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                log.warning("tag fetch error: %s", e)
    # Final keyword filter - Polymarket's tagging is noisy
    filtered = []
    for m in all_markets:
        title = (m.get("_event_title") or "") + " " + (m.get("question") or "")
        tl = title.lower()
        if any(k in tl for k in DISASTER_KEYWORDS):
            filtered.append(m)
    log.info("Fetched %d disaster markets (from %d candidates)", len(filtered), len(all_markets))
    _cache.put("polymarket_disasters", filtered)
    return filtered


if __name__ == "__main__":
    import json
    rows = fetch_disaster_markets()
    print(f"Got {len(rows)} markets")
    for r in rows[:5]:
        print("-", r.get("_event_title"), "->", r.get("question"))
    if rows:
        print(json.dumps({k: v for k, v in rows[0].items() if not k.startswith("_") or k in ("_event_title",)}, indent=2)[:1000])
