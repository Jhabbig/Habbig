from __future__ import annotations
import aiohttp
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from ._retry import fetch_json_with_retry


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

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

class PolymarketAggregator:
    """Fetches midterm election market data from Polymarket."""

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _fetch_politics_events(self, max_pages: int = 5) -> list[dict]:
        """Fetch politics events from Gamma API with page cap."""
        session = await self._get_session()
        markets = []
        offset = 0
        limit = 100
        page = 0

        while page < max_pages:
            url = f"{GAMMA_API}/events"
            params = {"tag_slug": "politics", "limit": limit, "offset": offset, "active": "true", "closed": "false"}
            data = await fetch_json_with_retry(
                session, url, params=params, timeout=15, source_label="polymarket-events",
            )
            if not data:
                break
            markets.extend(data)
            if len(data) < limit:
                break
            offset += limit
            page += 1

        logger.info(f"Polymarket fetched {len(markets)} politics events in {page + 1} pages")
        return markets

    async def fetch_election_markets(self) -> list[dict]:
        """Fetch US midterm election markets from Gamma API."""
        markets = await self._fetch_politics_events(max_pages=5)
        return self._normalize_markets(markets)

    async def fetch_price_history(self, token_id: str, interval: str = "1d", fidelity: int = 60) -> list[dict]:
        """Fetch historical prices for a token from CLOB API."""
        session = await self._get_session()
        url = f"{CLOB_API}/prices-history"
        params = {"market": token_id, "interval": interval, "fidelity": fidelity}
        data = await fetch_json_with_retry(
            session, url, params=params, timeout=15, source_label="polymarket-prices",
        )
        if not data:
            return []
        return [
            {
                "timestamp": point.get("t", 0),
                "price": float(point.get("p", 0)),
                "source": "polymarket",
            }
            for point in (data.get("history", []) if isinstance(data, dict) else data)
        ]

    async def fetch_orderbook(self, token_id: str) -> dict:
        """Fetch current orderbook for a token."""
        session = await self._get_session()
        url = f"{CLOB_API}/book"
        params = {"token_id": token_id}
        data = await fetch_json_with_retry(
            session, url, params=params, timeout=10, source_label="polymarket-book",
        )
        return data or {}

    def _normalize_markets(self, events: list[dict]) -> list[dict]:
        """Normalize Polymarket events into standardized market format."""
        normalized = []
        midterm_keywords = [
            "senate", "house", "governor", "midterm", "2026", "congress",
            "republican", "democrat", "gop", "election", "seat", "representative",
            "control", "majority", "flip"
        ]

        for event in events:
            title = (event.get("title") or "").lower()
            slug = (event.get("slug") or "").lower()
            description = (event.get("description") or "").lower()
            combined = f"{title} {slug} {description}"

            # Filter for midterm-relevant markets
            if not any(kw in combined for kw in midterm_keywords):
                continue

            # Determine race type
            race_type = "other"
            if "senate" in combined:
                race_type = "senate"
            elif "house" in combined or "representative" in combined:
                race_type = "house"
            elif "governor" in combined:
                race_type = "governor"
            elif "control" in combined or "majority" in combined:
                race_type = "control"

            # Extract state from title if possible
            state = self._extract_state(event.get("title", ""))

            # Process nested markets (outcomes)
            sub_markets = event.get("markets", [])
            for market in sub_markets:
                if not _is_current_open_market(
                    market.get("endDate"),
                    bool(market.get("closed", False)),
                    bool(market.get("active", True)),
                ):
                    continue
                outcomes = market.get("outcomes", "[]")
                if isinstance(outcomes, str):
                    import json
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = []

                prices_str = market.get("outcomePrices", "[]")
                if isinstance(prices_str, str):
                    import json
                    try:
                        prices = json.loads(prices_str)
                    except Exception:
                        prices = []
                else:
                    prices = prices_str or []

                token_ids = []
                clob_ids = market.get("clobTokenIds", "[]")
                if isinstance(clob_ids, str):
                    import json
                    try:
                        token_ids = json.loads(clob_ids)
                    except Exception:
                        token_ids = []
                else:
                    token_ids = clob_ids or []

                outcome_data = []
                for i, outcome in enumerate(outcomes):
                    price = None
                    if i < len(prices) and prices[i] is not None:
                        try:
                            price = float(prices[i])
                        except (TypeError, ValueError):
                            price = None
                    tid = token_ids[i] if i < len(token_ids) else None
                    outcome_data.append({
                        "name": outcome,
                        "probability": price,
                        "token_id": tid
                    })

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

    async def fetch_world_election_markets(self) -> list[dict]:
        """Fetch international/world election markets from Gamma API."""
        # Reuse the same fetched events (capped at 5 pages)
        markets = await self._fetch_politics_events(max_pages=5)
        return self._normalize_world_markets(markets)

    def _normalize_world_markets(self, events: list[dict]) -> list[dict]:
        """Normalize Polymarket events into world election market format."""
        import json as _json
        normalized = []

        # Must match an election-specific keyword
        election_keywords = [
            "president", "prime minister", "chancellor", "parliament",
            "coalition government", "election", "inaugurated", "reelect",
            "head of state", "ruling party", "opposition leader"
        ]
        # MUST mention a non-US country (required, not optional)
        country_keywords = [
            "uk ", "united kingdom", "britain", "france", "french",
            "germany", "german", "canada", "canadian", "australia",
            "australian", "brazil", "brazilian", "mexico", "mexican",
            "india", "indian", "japan", "japanese", "south korea", "korean",
            "italy", "italian", "spain", "spanish", "netherlands", "dutch",
            "israel", "israeli", "turkey", "turkish", "argentina",
            "colombian", "colombia", "poland", "polish", "ukraine",
            "ukrainian", "china", "chinese", "russia", "russian",
            "european", "eu ", "nato", "zelenskyy", "macron", "starmer",
            "trudeau", "modi", "lula", "meloni", "scholz"
        ]
        # Exclude US domestic politics
        us_exclude = [
            "senate", "house", "governor", "midterm", "congress",
            "representative", "seat", "supreme court", "state legislature",
            "us election", "american election", "trump", "biden",
            "harris", "desantis", "newsom"
        ]
        # Exclude clearly non-election topics
        noise_exclude = [
            "cricket", "nfl", "nba", "mlb", "soccer", "football match",
            "hurricane", "tropical", "earthquake", "refugee", "covid",
            "bitcoin", "crypto", "stock", "fed ", "interest rate",
            "weather", "temperature", "oscar", "grammy", "emmy"
        ]

        for event in events:
            title = (event.get("title") or "").lower()
            slug = (event.get("slug") or "").lower()
            description = (event.get("description") or "").lower()
            combined = f"{title} {slug} {description}"

            # Must match an election keyword
            if not any(kw in combined for kw in election_keywords):
                continue

            # Must mention a non-US country or leader
            if not any(kw in combined for kw in country_keywords):
                continue

            # Exclude US domestic politics
            if any(kw in combined for kw in us_exclude):
                continue

            # Exclude noise
            if any(kw in combined for kw in noise_exclude):
                continue

            country = self._extract_country(event.get("title", ""))

            sub_markets = event.get("markets", [])
            for market in sub_markets:
                if not _is_current_open_market(
                    market.get("endDate"),
                    bool(market.get("closed", False)),
                    bool(market.get("active", True)),
                ):
                    continue
                outcomes = market.get("outcomes", "[]")
                if isinstance(outcomes, str):
                    try:
                        outcomes = _json.loads(outcomes)
                    except Exception:
                        outcomes = []

                prices_str = market.get("outcomePrices", "[]")
                if isinstance(prices_str, str):
                    try:
                        prices = _json.loads(prices_str)
                    except Exception:
                        prices = []
                else:
                    prices = prices_str or []

                token_ids = []
                clob_ids = market.get("clobTokenIds", "[]")
                if isinstance(clob_ids, str):
                    try:
                        token_ids = _json.loads(clob_ids)
                    except Exception:
                        token_ids = []
                else:
                    token_ids = clob_ids or []

                outcome_data = []
                for i, outcome in enumerate(outcomes):
                    price = None
                    if i < len(prices) and prices[i] is not None:
                        try:
                            price = float(prices[i])
                        except (TypeError, ValueError):
                            price = None
                    tid = token_ids[i] if i < len(token_ids) else None
                    outcome_data.append({
                        "name": outcome,
                        "probability": price,
                        "token_id": tid
                    })

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

    def _extract_country(self, title: str) -> Optional[str]:
        """Try to extract country code from market title."""
        # Order matters: check longer/more specific names first to avoid
        # e.g. "ukraine" matching "uk"
        countries = [
            ("united kingdom", "UK"), ("britain", "UK"), ("british", "UK"),
            ("ukraine", "UA"), ("ukrainian", "UA"),
            ("france", "FR"), ("french", "FR"),
            ("germany", "DE"), ("german", "DE"),
            ("canada", "CA"), ("canadian", "CA"),
            ("australia", "AU"), ("australian", "AU"),
            ("brazil", "BR"), ("brazilian", "BR"),
            ("mexico", "MX"), ("mexican", "MX"),
            ("india", "IN"), ("indian", "IN"),
            ("japan", "JP"), ("japanese", "JP"),
            ("south korea", "KR"), ("korean", "KR"),
            ("italy", "IT"), ("italian", "IT"),
            ("spain", "ES"), ("spanish", "ES"),
            ("netherlands", "NL"), ("dutch", "NL"),
            ("israel", "IL"), ("israeli", "IL"),
            ("turkey", "TR"), ("turkish", "TR"),
            ("argentina", "AR"), ("argentine", "AR"),
            ("colombia", "CO"), ("colombian", "CO"),
            ("poland", "PL"), ("polish", "PL"),
            ("china", "CN"), ("chinese", "CN"),
            ("russia", "RU"), ("russian", "RU"),
            ("european union", "EU"), ("eu ", "EU"),
        ]
        title_lower = title.lower()
        for name, code in countries:
            if name in title_lower:
                return code
        # Check for " UK " as a word boundary (avoids matching "ukraine")
        if " uk " in f" {title_lower} " and "ukrain" not in title_lower:
            return "UK"
        return None

    def _extract_state(self, title: str) -> Optional[str]:
        """Try to extract US state from market title."""
        states = {
            "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
            "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
            "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
            "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
            "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
            "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
            "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
            "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
            "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
            "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
            "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
            "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
            "Wisconsin": "WI", "Wyoming": "WY"
        }
        # Check full state names first. Use word-boundary regex so e.g.
        # "New York" doesn't shadow "York"-something, and explicitly skip
        # the "Washington" → WA mapping when the title is about Washington
        # D.C. (which isn't a state).
        title_lower = title.lower()
        is_dc_title = "washington d.c." in title_lower or "washington, d.c." in title_lower
        for name, abbr in states.items():
            name_lower = name.lower()
            if name_lower == "washington" and is_dc_title:
                continue
            if re.search(rf"\b{re.escape(name_lower)}\b", title_lower):
                return abbr
        # Only check abbreviations that won't match common English words
        ambiguous_abbrs = {"IN", "OR", "ME", "OH", "AL", "OK", "HI", "ID", "PA", "MA"}
        for name, abbr in states.items():
            if abbr not in ambiguous_abbrs and f" {abbr} " in f" {title} ":
                return abbr
        return None
