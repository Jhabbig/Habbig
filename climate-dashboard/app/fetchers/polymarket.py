"""Polymarket Gamma climate-tagged markets.

Pagination → keyword denylist → keyword allowlist. Polymarket's tagging is
noisy (sports / politics / crypto markets get tagged with climate tags), so
even after the gamma fetch we apply a strict climate keyword filter.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from .. import cache, http

logger = logging.getLogger("climate.polymarket")

GAMMA_BASE = "https://gamma-api.polymarket.com"

CLIMATE_TAG_SLUGS = (
    "climate-change",
    "global-temperature",
    "climate",
    "global-warming",
    "sea-level",
    "extreme-weather",
)

# Sports / politics / crypto markets share keywords with climate. Note: do not
# add "vs." here — climate markets sometimes phrase comparisons (e.g. "Arctic
# vs Antarctic") with that token.
REJECT_KEYWORDS = (
    "nfl", "nba", "nhl", "mlb", "mls", "rugby", "premier league", "ligue 1",
    "champion", "playoff", "election", "president", "senate", "governor",
    "ipo", "stock", "bitcoin", "crypto", "tesla", "spacex", "starship",
    "head-to-head", "champions league", "fight", "boxing",
)

CLIMATE_KEYWORDS = (
    "warmest", "hottest year", "global temperature", "global average",
    "climate", "co2", "carbon dioxide", "ppm", "sea ice", "arctic",
    "antarctic", "sea level", "ipcc", "1.5", "2 degrees", "paris agreement",
    "el nino", "la nina", "enso", "ocean temperature", "sst",
)


def _fetch_events_by_tag(tag_slug: str, seen_ids: set, all_markets: list,
                         lock: threading.Lock) -> None:
    offset = 0
    for _ in range(8):  # cap pagination
        r = http.get(
            f"{GAMMA_BASE}/events",
            params={"tag_slug": tag_slug, "closed": "false",
                    "limit": "100", "offset": str(offset)},
        )
        if not r:
            break
        try:
            events = r.json()
        except Exception:
            break
        if not events:
            break
        for event in events:
            title = (event.get("title", "") or "")
            tl = title.lower()
            if any(k in tl for k in REJECT_KEYWORDS):
                continue
            tags = event.get("tags", [])
            tag_labels = [t.get("label", "") for t in tags if isinstance(t, dict)]
            for m in event.get("markets", []):
                mid = m.get("conditionId") or m.get("id", "")
                if not mid:
                    continue
                with lock:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    m["_event_title"] = title
                    m["_event_tags"] = tag_labels
                    all_markets.append(m)
        offset += 100


def fetch() -> list[dict]:
    cached = cache.get("polymarket")
    if cached is not None:
        return cached
    all_markets: list[dict] = []
    seen_ids: set = set()
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_fetch_events_by_tag, slug, seen_ids, all_markets, lock)
                   for slug in CLIMATE_TAG_SLUGS]
        for f in futures:
            try:
                f.result()
            except Exception as e:
                logger.warning("tag fetch error: %s", e)
    filtered = []
    for m in all_markets:
        title = (m.get("_event_title") or "") + " " + (m.get("question") or "")
        tl = title.lower()
        if (any(k in tl for k in CLIMATE_KEYWORDS)
                or any("climate" in t.lower() for t in m.get("_event_tags", []))):
            filtered.append(m)
    logger.info("Fetched %d climate markets (from %d candidates)",
                len(filtered), len(all_markets))
    cache.set("polymarket", filtered)
    return filtered
