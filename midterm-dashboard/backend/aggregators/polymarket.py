from __future__ import annotations
import aiohttp
import asyncio
import json as _json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from data_sources.countries import COUNTRIES, country_name
from data_sources.fips import STATE_NAMES, STATE_FIPS

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Polymarket exposes structured tags on events. These slugs reliably mark a
# market as election-relevant — far more accurate than substring keyword soup
# on titles. Tag slugs are case-insensitive on the API side.
ELECTION_TAG_SLUGS: set[str] = {
    "elections", "us-elections", "midterm-elections", "2026-midterms",
    "senate", "house", "governor", "presidential", "primary",
    "world-elections", "international-elections",
}

# Tag → race_type mapping. First match wins.
RACE_TYPE_BY_TAG: list[tuple[str, str]] = [
    ("senate", "senate"),
    ("house", "house"),
    ("governor", "governor"),
    ("presidential", "presidential"),
    ("primary", "primary"),
    ("congressional-control", "control"),
    ("world-elections", "world"),
    ("international-elections", "world"),
]

# Title fallbacks (only used when no structured tag yields a race_type).
_RACE_TYPE_FALLBACK_TITLE_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("senate",), "senate"),
    (("house", "representative"), "house"),
    (("governor",), "governor"),
    (("president",), "presidential"),
    (("primary", "nomination", "nominee"), "primary"),
    (("control", "majority"), "control"),
]

# US-domestic exclusion keywords for the world-elections filter — built once
# from STATE_NAMES so adding/removing states stays in sync automatically.
_US_DOMESTIC_KEYWORDS: set[str] = {
    "senate", "house", "governor", "midterm", "congress",
    "representative", "seat", "supreme court", "state legislature",
    "us election", "american election",
}
_US_DOMESTIC_KEYWORDS.update(name.lower() for name in STATE_NAMES.values())

# Election keywords for the world filter — these don't need to be exhaustive
# because we also require a tag-based world signal as the primary filter.
_WORLD_ELECTION_KEYWORDS = {
    "president", "prime minister", "chancellor", "parliament",
    "coalition", "election", "inaugurated", "reelect",
    "ruling party", "opposition leader",
}


def _is_current_open_market(end_date_str: Optional[str], closed: bool, active: bool, max_years_out: float = 3.0) -> bool:
    """Return True only if market is open and has a near-term end date."""
    if closed:
        return False
    if active is False:
        return False
    if not end_date_str:
        return True
    try:
        s = end_date_str.replace("Z", "+00:00")
        end_dt = datetime.fromisoformat(s)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    now = datetime.now(timezone.utc)
    if end_dt < now:
        return False
    if end_dt > now + timedelta(days=365 * max_years_out):
        return False
    return True


def _tag_slugs(event: dict) -> set[str]:
    """Return the set of tag slugs on an event, lowercased."""
    tags = event.get("tags") or []
    out: set[str] = set()
    for t in tags:
        if isinstance(t, dict):
            slug = (t.get("slug") or t.get("label") or "").lower().strip()
            if slug:
                out.add(slug)
        elif isinstance(t, str):
            out.add(t.lower().strip())
    return out


def _race_type_from_tags(tag_slugs: set[str]) -> Optional[str]:
    for slug, rt in RACE_TYPE_BY_TAG:
        if slug in tag_slugs:
            return rt
    return None


def _race_type_from_title(text: str) -> str:
    for keywords, rt in _RACE_TYPE_FALLBACK_TITLE_KEYWORDS:
        if any(kw in text for kw in keywords):
            return rt
    return "other"


def _parse_outcomes(market: dict) -> tuple[list[str], list[Optional[float]], list[Optional[str]]]:
    """Extract (outcomes, prices, token_ids) from a Polymarket market dict.

    All three fields ship as JSON-encoded strings on the API. Returns parallel
    lists indexed by outcome.
    """
    def _decode(field, default):
        raw = market.get(field, default)
        if isinstance(raw, str):
            try:
                return _json.loads(raw)
            except Exception:
                return []
        return raw or []

    outcomes = _decode("outcomes", "[]")
    prices_raw = _decode("outcomePrices", "[]")
    token_ids = _decode("clobTokenIds", "[]")

    prices: list[Optional[float]] = []
    for i in range(len(outcomes)):
        if i < len(prices_raw) and prices_raw[i] is not None:
            try:
                prices.append(float(prices_raw[i]))
            except (TypeError, ValueError):
                prices.append(None)
        else:
            prices.append(None)

    tids: list[Optional[str]] = []
    for i in range(len(outcomes)):
        tids.append(token_ids[i] if i < len(token_ids) else None)

    return list(outcomes), prices, tids


class PolymarketAggregator:
    """Fetches midterm election market data from Polymarket."""

    # Cache events for 4 minutes. The data refresh loop runs every 5 minutes
    # and calls fetch_election_markets + fetch_world_election_markets back to
    # back, so a single fetch covers both calls without re-hitting the API.
    _EVENT_CACHE_TTL = 240

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._session = session
        self._owns_session = session is None
        self._cached_events: list[dict] = []
        self._cache_time: float = 0.0

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _fetch_politics_events(self) -> list[dict]:
        """Fetch politics events from Gamma API with no hard page cap.

        Cached for ``_EVENT_CACHE_TTL`` seconds so subsequent calls within a
        refresh cycle (``fetch_election_markets`` + ``fetch_world_election_markets``)
        share the same fetched events.
        """
        now = time.time()
        if self._cached_events and (now - self._cache_time) < self._EVENT_CACHE_TTL:
            return self._cached_events

        session = await self._get_session()
        events: list[dict] = []
        offset = 0
        limit = 100
        # Hard ceiling to avoid runaway loops if the API ever stops paginating
        # cleanly. 50 pages × 100 = 5,000 events is well above any realistic
        # politics catalog size on Polymarket.
        max_pages = 50

        for page in range(max_pages):
            try:
                url = f"{GAMMA_API}/events"
                params = {
                    "tag_slug": "politics",
                    "limit": limit,
                    "offset": offset,
                    "active": "true",
                    "closed": "false",
                }
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        logger.warning("Polymarket rate limited, backing off")
                        await asyncio.sleep(2)
                        continue
                    if resp.status != 200:
                        logger.error(f"Polymarket API error: {resp.status}")
                        break
                    data = await resp.json()
                    if not data:
                        break
                    events.extend(data)
                    if len(data) < limit:
                        break
                    offset += limit
            except Exception as e:
                logger.error(f"Polymarket fetch error at offset {offset}: {e}")
                break

        logger.info(f"Polymarket fetched {len(events)} politics events")
        self._cached_events = events
        self._cache_time = now
        return events

    async def fetch_election_markets(self) -> list[dict]:
        """Fetch US midterm election markets from Gamma API."""
        events = await self._fetch_politics_events()
        return self._normalize_markets(events)

    async def fetch_world_election_markets(self) -> list[dict]:
        """Fetch international election markets from Gamma API.

        Reuses the cached events from ``fetch_election_markets`` instead of
        re-paginating the entire politics catalog.
        """
        events = await self._fetch_politics_events()
        return self._normalize_world_markets(events)

    async def fetch_price_history(self, token_id: str, interval: str = "1d", fidelity: int = 60) -> list[dict]:
        """Fetch historical prices for a token from CLOB API."""
        session = await self._get_session()
        try:
            url = f"{CLOB_API}/prices-history"
            params = {"market": token_id, "interval": interval, "fidelity": fidelity}
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [
                    {
                        "timestamp": point.get("t", 0),
                        "price": float(point.get("p", 0)),
                        "source": "polymarket",
                    }
                    for point in (data.get("history", []) if isinstance(data, dict) else data)
                ]
        except Exception as e:
            logger.error(f"Polymarket price history error: {e}")
            return []

    async def fetch_orderbook(self, token_id: str) -> dict:
        """Fetch current orderbook for a token."""
        session = await self._get_session()
        try:
            url = f"{CLOB_API}/book"
            params = {"token_id": token_id}
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {}
                return await resp.json()
        except Exception as e:
            logger.error(f"Polymarket orderbook error: {e}")
            return {}

    def _normalize_markets(self, events: list[dict]) -> list[dict]:
        """Normalize Polymarket events into US midterm markets."""
        normalized = []

        for event in events:
            tag_slugs = _tag_slugs(event)
            title = (event.get("title") or "").lower()
            slug = (event.get("slug") or "").lower()
            description = (event.get("description") or "").lower()
            combined = f"{title} {slug} {description}"

            # Election relevance: prefer tag-based detection. Fall back to a
            # narrow keyword set only if the event has no usable tags at all.
            is_election_tagged = bool(tag_slugs & ELECTION_TAG_SLUGS)
            if not is_election_tagged:
                fallback_keywords = (
                    "senate", "house", "governor", "midterm", "2026",
                    "congress", "election", "primary",
                )
                if not any(kw in combined for kw in fallback_keywords):
                    continue

            # Skip world-election-tagged events here — they belong to the
            # international set.
            if "world-elections" in tag_slugs or "international-elections" in tag_slugs:
                continue

            race_type = _race_type_from_tags(tag_slugs) or _race_type_from_title(combined)
            # World tag would have been excluded above, but defend against
            # mis-tagged events surfacing as US races.
            if race_type == "world":
                continue

            state = self._extract_state(event.get("title", ""))

            for market in event.get("markets", []) or []:
                if not _is_current_open_market(
                    market.get("endDate"),
                    bool(market.get("closed", False)),
                    bool(market.get("active", True)),
                ):
                    continue

                outcomes, prices, token_ids = _parse_outcomes(market)
                outcome_data = [
                    {"name": outcomes[i], "probability": prices[i], "token_id": token_ids[i]}
                    for i in range(len(outcomes))
                ]

                normalized.append({
                    "source": "polymarket",
                    "source_id": market.get("id", ""),
                    "event_id": event.get("id", ""),
                    "title": market.get("question") or event.get("title", ""),
                    "event_title": event.get("title", ""),
                    "slug": event.get("slug", ""),
                    "race_type": race_type,
                    "state": state,
                    "outcomes": outcome_data,
                    "volume": float(market.get("volume", 0) or 0),
                    "liquidity": float(market.get("liquidity", 0) or 0),
                    "active": market.get("active", True),
                    "closed": market.get("closed", False),
                    "end_date": market.get("endDate"),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                })

        return normalized

    def _normalize_world_markets(self, events: list[dict]) -> list[dict]:
        """Normalize Polymarket events into world election markets.

        Primary filter: ``world-elections`` / ``international-elections`` tag.
        For events without those tags, fall back to a (country-keyword AND
        election-keyword AND NOT US-domestic) heuristic.
        """
        normalized = []

        for event in events:
            tag_slugs = _tag_slugs(event)
            title = (event.get("title") or "").lower()
            slug = (event.get("slug") or "").lower()
            description = (event.get("description") or "").lower()
            combined = f"{title} {slug} {description}"

            world_tagged = ("world-elections" in tag_slugs) or ("international-elections" in tag_slugs)

            if not world_tagged:
                # Fallback: must hit an election keyword AND mention a known
                # non-US country, AND NOT mention US-domestic markers.
                if not any(kw in combined for kw in _WORLD_ELECTION_KEYWORDS):
                    continue
                country = self._extract_country(event.get("title", ""))
                if not country:
                    continue
                if any(kw in combined for kw in _US_DOMESTIC_KEYWORDS):
                    continue
            else:
                country = self._extract_country(event.get("title", ""))

            for market in event.get("markets", []) or []:
                if not _is_current_open_market(
                    market.get("endDate"),
                    bool(market.get("closed", False)),
                    bool(market.get("active", True)),
                ):
                    continue

                outcomes, prices, token_ids = _parse_outcomes(market)
                outcome_data = [
                    {"name": outcomes[i], "probability": prices[i], "token_id": token_ids[i]}
                    for i in range(len(outcomes))
                ]

                normalized.append({
                    "source": "polymarket",
                    "source_id": market.get("id", ""),
                    "event_id": event.get("id", ""),
                    "title": market.get("question") or event.get("title", ""),
                    "event_title": event.get("title", ""),
                    "slug": event.get("slug", ""),
                    "race_type": "world",
                    "state": country,
                    "outcomes": outcome_data,
                    "volume": float(market.get("volume", 0) or 0),
                    "liquidity": float(market.get("liquidity", 0) or 0),
                    "active": market.get("active", True),
                    "closed": market.get("closed", False),
                    "end_date": market.get("endDate"),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                })

        return normalized

    @staticmethod
    def _extract_country(title: str) -> Optional[str]:
        """Extract an ISO-2 country code from a market title.

        Built from the shared ``COUNTRIES`` table so adding a country in one
        place updates this matcher automatically. Demonyms are also matched
        (e.g. "French" → FR) via the ``COUNTRY_ADJECTIVES`` table.
        """
        if not title:
            return None
        title_lower = title.lower()

        # Match full country names first (longer = more specific). Sort by
        # length descending so e.g. "United Kingdom" beats "Kingdom".
        sorted_countries = sorted(
            COUNTRIES.items(),
            key=lambda item: len(item[1][1]),
            reverse=True,
        )
        for code, (_iso3, name) in sorted_countries:
            if name.lower() in title_lower:
                return code

        # Demonyms (e.g. "British", "Hungarian"). Import lazily so the module
        # stays importable even if the demonym table grows huge.
        from data_sources.countries import COUNTRY_ADJECTIVES
        sorted_adjs = sorted(COUNTRY_ADJECTIVES.items(), key=lambda kv: len(kv[1]), reverse=True)
        for code, adj in sorted_adjs:
            # Word-boundary match avoids "Italian" matching inside "Italianate"
            if re.search(rf"\b{re.escape(adj.lower())}\b", title_lower):
                return code

        # "UK" as a whole word, but not inside "ukraine"
        if re.search(r"\buk\b", title_lower) and "ukrain" not in title_lower:
            return "UK"

        return None

    @staticmethod
    def _extract_state(title: str) -> Optional[str]:
        """Extract a US state code from a market title.

        Built from the shared ``STATE_NAMES`` and ``STATE_FIPS`` tables in
        ``data_sources.fips`` rather than a hardcoded copy.
        """
        if not title:
            return None

        title_lower = title.lower()
        is_dc_title = "washington d.c." in title_lower or "washington, d.c." in title_lower

        # Full state names — sort by length descending so "New Mexico" beats
        # "Mexico", "North Carolina" beats "Carolina", etc.
        sorted_states = sorted(STATE_NAMES.items(), key=lambda kv: len(kv[1]), reverse=True)
        for abbr, name in sorted_states:
            name_lower = name.lower()
            if name_lower == "washington" and is_dc_title:
                continue
            if re.search(rf"\b{re.escape(name_lower)}\b", title_lower):
                return abbr

        # Postal abbreviations: only safe ones. Many US state codes collide
        # with common English words ("IN", "OR", "ME", "OH", "AL", "OK", "HI",
        # "ID", "PA", "MA"), so skip those.
        ambiguous_abbrs = {"IN", "OR", "ME", "OH", "AL", "OK", "HI", "ID", "PA", "MA", "AK", "AR", "DE"}
        padded = f" {title} "
        for abbr in STATE_FIPS:
            if abbr in ambiguous_abbrs:
                continue
            if f" {abbr} " in padded:
                return abbr

        return None
