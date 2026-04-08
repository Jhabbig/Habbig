from __future__ import annotations
import aiohttp
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional


def _is_current_open_market(end_date_str: Optional[str], status: Optional[str], max_years_out: float = 3.0) -> bool:
    """Return True only if market is open and has a near-term end date."""
    if status == "closed":
        return False
    if not end_date_str:
        return True  # keep if unknown, but status check already passed
    try:
        # Normalize Z suffix
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

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

class KalshiAggregator:
    """Fetches election market data from Kalshi."""

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._session = session
        self._owns_session = session is None
        self._cached_events: list[dict] = []
        self._cached_markets: dict[str, list[dict]] = {}
        self._cache_time: float = 0

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _fetch_events_and_markets(self) -> tuple[list[dict], dict[str, list[dict]]]:
        """Fetch events + their nested markets, cached for 4 minutes."""
        now = time.time()
        if self._cached_events and (now - self._cache_time) < 240:
            return self._cached_events, self._cached_markets

        session = await self._get_session()
        all_events = []
        all_markets: dict[str, list[dict]] = {}

        # Fetch all events (paginated)
        cursor = None
        pages = 0
        while pages < 10:
            try:
                url = f"{KALSHI_API}/events"
                params = {"limit": 100, "status": "open"}
                if cursor:
                    params["cursor"] = cursor
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        logger.warning("Kalshi events rate limited")
                        await asyncio.sleep(2)
                        continue
                    if resp.status != 200:
                        logger.error(f"Kalshi events API error: {resp.status}")
                        break
                    data = await resp.json()
                    events = data.get("events", [])
                    if not events:
                        break
                    all_events.extend(events)
                    cursor = data.get("cursor")
                    if not cursor:
                        break
                    pages += 1
            except Exception as e:
                logger.error(f"Kalshi events fetch error: {e}")
                break

        logger.info(f"Kalshi fetched {len(all_events)} events in {pages + 1} pages")

        # Filter to truly relevant election/politics events
        # Prioritize events with election-specific tickers or titles
        high_priority = []
        low_priority = []
        for e in all_events:
            cat = (e.get("category") or "").lower()
            if cat not in ("elections", "politics"):
                continue
            ticker = (e.get("event_ticker") or e.get("ticker") or "").upper()
            title = (e.get("title") or "").lower()

            # High priority: actual races (senate, governor, president, world leaders)
            is_high = (
                ticker.startswith("SENATE") or ticker.startswith("GOVPARTY") or
                ticker.startswith("POWER") or ticker.startswith("KXPRES") or
                "senate" in title or "governor" in title or
                "president" in title or "prime minister" in title or
                "pope" in title or "g7" in title or "election" in title or
                "leader" in title or "successor" in title
            )
            if is_high:
                high_priority.append(e)
            else:
                low_priority.append(e)

        # Fetch high priority first, then low priority up to a limit
        relevant_events = high_priority + low_priority[:50]
        logger.info(f"Kalshi: {len(high_priority)} high-priority + {min(len(low_priority), 50)} low-priority events (from {len(all_events)} total)")

        # Fetch markets for relevant events with rate-limit backoff
        rate_limit_hits = 0
        for event in relevant_events:
            event_ticker = event.get("event_ticker") or event.get("ticker")
            if not event_ticker:
                continue
            if rate_limit_hits >= 5:
                logger.warning("Kalshi too many rate limits, stopping market fetch")
                break
            try:
                url = f"{KALSHI_API}/markets"
                params = {"event_ticker": event_ticker, "limit": 200}
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 429:
                        rate_limit_hits += 1
                        await asyncio.sleep(5)
                        # Retry this event once after backoff
                        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as retry_resp:
                            if retry_resp.status == 200:
                                data = await retry_resp.json()
                                markets = data.get("markets", [])
                                if markets:
                                    for m in markets:
                                        m["_event_category"] = event.get("category", "")
                                        m["_event_title"] = event.get("title", "")
                                    all_markets[event_ticker] = markets
                        continue
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    markets = data.get("markets", [])
                    if markets:
                        for m in markets:
                            m["_event_category"] = event.get("category", "")
                            m["_event_title"] = event.get("title", "")
                        all_markets[event_ticker] = markets
            except Exception as e:
                logger.debug(f"Kalshi market fetch for {event_ticker}: {e}")
            # Delay to avoid rate limits
            await asyncio.sleep(0.3)

        total_markets = sum(len(v) for v in all_markets.values())
        logger.info(f"Kalshi fetched {total_markets} markets across {len(all_markets)} events")

        self._cached_events = all_events
        self._cached_markets = all_markets
        self._cache_time = time.time()
        return all_events, all_markets

    async def fetch_election_markets(self) -> list[dict]:
        """Fetch US election-related markets from Kalshi."""
        events, markets_by_event = await self._fetch_events_and_markets()
        all_markets = []
        for event_ticker, markets in markets_by_event.items():
            for m in markets:
                if self._is_us_election_market(m):
                    all_markets.append(m)
        logger.info(f"Kalshi US election: {len(all_markets)} markets")
        return self._normalize_markets(all_markets)

    async def fetch_world_election_markets(self) -> list[dict]:
        """Fetch international/world election markets from Kalshi."""
        events, markets_by_event = await self._fetch_events_and_markets()
        all_markets = []
        for event_ticker, markets in markets_by_event.items():
            for m in markets:
                if self._is_world_election_market(m):
                    all_markets.append(m)
        logger.info(f"Kalshi world election: {len(all_markets)} markets")
        return self._normalize_world_markets(all_markets)

    async def fetch_orderbook(self, ticker: str) -> dict:
        """Fetch orderbook for a specific market."""
        session = await self._get_session()
        try:
            url = f"{KALSHI_API}/markets/{ticker}/orderbook"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {}
                return await resp.json()
        except Exception as e:
            logger.error(f"Kalshi orderbook error: {e}")
            return {}

    async def fetch_market_history(self, ticker: str) -> list[dict]:
        """Fetch trade history for a market."""
        session = await self._get_session()
        try:
            url = f"{KALSHI_API}/markets/{ticker}/trades"
            params = {"limit": 1000}
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                trades = data.get("trades", [])
                return [
                    {
                        "timestamp": t.get("created_time", ""),
                        "price": t.get("yes_price", 0) / 100 if t.get("yes_price") else 0,
                        "source": "kalshi"
                    }
                    for t in trades
                ]
        except Exception as e:
            logger.error(f"Kalshi history error: {e}")
            return []

    def _is_us_election_market(self, market: dict) -> bool:
        """Check if a market is a US election market."""
        title = (market.get("title") or "").lower()
        subtitle = (market.get("subtitle") or "").lower()
        event_title = (market.get("_event_title") or "").lower()
        ticker = (market.get("ticker") or market.get("event_ticker") or "").upper()
        combined = f"{title} {subtitle} {event_title}"

        # Ticker-based matching (very reliable)
        us_ticker_prefixes = [
            "SENATE", "GOVPARTY", "POWER-", "KXPRESPERSON", "KXPRESPARTY",
            "KXNEXTSPEAKER", "KXCAPCONTROL", "KXTERMLIMITS"
        ]
        if any(ticker.startswith(p) for p in us_ticker_prefixes):
            return True

        us_keywords = [
            "senate winner", "senate", "governor winner", "governor",
            "house", "midterm", "congress", "presidential election",
            "president of the united states", "speaker of the house",
            "supreme court", "attorney general", "secretary of"
        ]
        # Exclude world markets from this filter
        world_exclude = [
            "china", "chinese", "france", "germany", "uk ", "united kingdom",
            "israel", "africa", "g7", "eu ", "european"
        ]
        if any(kw in combined for kw in world_exclude):
            return False

        return any(kw in combined for kw in us_keywords)

    def _is_world_election_market(self, market: dict) -> bool:
        """Check if a market is an international election market."""
        category = (market.get("_event_category") or market.get("category") or "").lower()
        title = (market.get("title") or "").lower()
        subtitle = (market.get("subtitle") or "").lower()
        event_title = (market.get("_event_title") or "").lower()
        combined = f"{title} {subtitle} {event_title}"

        # Must be in elections/politics/world category
        if category not in ("elections", "politics", "world"):
            return False

        # Exclude US-specific
        us_exclude = [
            "united states", "senate", "house", "governor", "midterm",
            "congress", "representative", "state legislature"
        ]
        if any(kw in combined for kw in us_exclude):
            return False

        # Must mention international politics
        world_keywords = [
            "president", "prime minister", "chancellor", "parliament",
            "election", "leader", "pope", "g7", "g20",
            "germany", "france", "united kingdom", "canada", "australia",
            "brazil", "mexico", "india", "japan", "south korea",
            "italy", "spain", "netherlands", "israel", "turkey",
            "argentina", "colombia", "poland", "china", "chinese",
            "russia", "russian", "ukraine", "african", "africa",
            "communist party", "successor", "leave office"
        ]
        return any(kw in combined for kw in world_keywords)

    def _normalize_world_markets(self, markets: list[dict]) -> list[dict]:
        """Normalize Kalshi world election markets."""
        normalized = []
        for m in markets:
            end_date = m.get("expiration_time") or m.get("close_time")
            if not _is_current_open_market(end_date, m.get("status")):
                continue
            if self._is_sports_junk(m):
                continue
            title = m.get("title", "")
            subtitle = m.get("subtitle", "")

            yes_price = m.get("yes_bid", 0) or m.get("last_price", 0) or 0
            no_price = m.get("no_bid", 0) or 0

            if isinstance(yes_price, (int, float)) and yes_price > 1:
                yes_price = yes_price / 100
            if isinstance(no_price, (int, float)) and no_price > 1:
                no_price = no_price / 100

            country = self._extract_country(title + " " + (m.get("_event_title") or ""))

            outcomes = [
                {"name": "Yes", "probability": yes_price, "token_id": None},
                {"name": "No", "probability": no_price if no_price is not None else (1 - yes_price if yes_price is not None else None), "token_id": None}
            ]
            if m.get("yes_sub_title"):
                outcomes[0]["name"] = m["yes_sub_title"]
            if m.get("no_sub_title"):
                outcomes[1]["name"] = m["no_sub_title"]

            normalized.append({
                "source": "kalshi",
                "source_id": m.get("ticker", ""),
                "event_id": m.get("event_ticker", ""),
                "title": title,
                "event_title": m.get("_event_title") or subtitle or title,
                "slug": m.get("ticker", "").lower(),
                "race_type": "world",
                "state": country,
                "outcomes": outcomes,
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("open_interest", 0) or 0),
                "active": m.get("status", "open") != "closed",
                "closed": m.get("status") == "closed",
                "end_date": m.get("expiration_time") or m.get("close_time"),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })
        return normalized

    def _is_sports_junk(self, market: dict) -> bool:
        """Detect sports parlays and non-election markets that leaked through."""
        ticker = (market.get("ticker") or market.get("event_ticker") or "").upper()
        title = (market.get("title") or "").lower()
        event_title = (market.get("_event_title") or "").lower()
        combined = f"{title} {event_title}"
        sports_tickers = ("KXMVE", "KXSPORTS", "KXNBA", "KXNFL", "KXMLB", "KXNHL",
                          "KXSOCCER", "KXTENNIS", "KXMMA", "KXGOLF", "KXNCAA")
        if any(ticker.startswith(p) for p in sports_tickers):
            return True
        sports_terms = ["nba", "nfl", "mlb", "nhl", "goals scored", "points scored",
                        "wins by over", "touchdown", "home run", "penalty kick",
                        "assists", "rebounds", "strikeout", "bundesliga", "la liga",
                        "premier league", "serie a", "champions league"]
        if any(t in combined for t in sports_terms):
            return True
        return False

    def _normalize_markets(self, markets: list[dict]) -> list[dict]:
        """Normalize Kalshi US election markets."""
        normalized = []
        for m in markets:
            end_date = m.get("expiration_time") or m.get("close_time")
            if not _is_current_open_market(end_date, m.get("status")):
                continue
            if self._is_sports_junk(m):
                continue
            title = m.get("title", "")
            subtitle = m.get("subtitle", "")
            combined = f"{title} {subtitle}".lower()

            race_type = "other"
            if "senate" in combined:
                race_type = "senate"
            elif "house" in combined or "representative" in combined:
                race_type = "house"
            elif "governor" in combined:
                race_type = "governor"
            elif "control" in combined or "majority" in combined:
                race_type = "control"
            elif "president" in combined:
                race_type = "presidential"

            yes_price = m.get("yes_bid", 0) or m.get("last_price", 0) or 0
            no_price = m.get("no_bid", 0) or 0

            if isinstance(yes_price, (int, float)) and yes_price > 1:
                yes_price = yes_price / 100
            if isinstance(no_price, (int, float)) and no_price > 1:
                no_price = no_price / 100

            state = self._extract_state(title)

            outcomes = [
                {"name": "Yes", "probability": yes_price, "token_id": None},
                {"name": "No", "probability": no_price if no_price is not None else (1 - yes_price if yes_price is not None else None), "token_id": None}
            ]
            if m.get("yes_sub_title"):
                outcomes[0]["name"] = m["yes_sub_title"]
            if m.get("no_sub_title"):
                outcomes[1]["name"] = m["no_sub_title"]

            normalized.append({
                "source": "kalshi",
                "source_id": m.get("ticker", ""),
                "event_id": m.get("event_ticker", ""),
                "title": title,
                "event_title": m.get("_event_title") or subtitle or title,
                "slug": m.get("ticker", "").lower(),
                "race_type": race_type,
                "state": state,
                "outcomes": outcomes,
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("open_interest", 0) or 0),
                "active": m.get("status", "open") != "closed",
                "closed": m.get("status") == "closed",
                "end_date": m.get("expiration_time") or m.get("close_time"),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })
        return normalized

    def _extract_country(self, title: str) -> Optional[str]:
        """Try to extract country code from market title."""
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
            ("south africa", "ZA"), ("african union", "AU"),
            ("european union", "EU"), ("eu ", "EU"),
        ]
        title_lower = title.lower()
        for name, code in countries:
            if name in title_lower:
                return code
        if " uk " in f" {title_lower} " and "ukrain" not in title_lower:
            return "UK"
        return None

    def _extract_state(self, title: str) -> Optional[str]:
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
        # Check full state names first (no false positives)
        title_lower = title.lower()
        for name, abbr in states.items():
            if name.lower() in title_lower:
                return abbr
        # Only check abbreviations that won't match common English words
        ambiguous_abbrs = {"IN", "OR", "ME", "OH", "AL", "OK", "HI", "ID", "PA", "MA"}
        for name, abbr in states.items():
            if abbr not in ambiguous_abbrs and f" {abbr} " in f" {title} ":
                return abbr
        return None
