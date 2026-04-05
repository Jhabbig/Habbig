import aiohttp
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

PREDICTIT_API = "https://www.predictit.org/api/marketdata/all/"

class PredictItAggregator:
    """Fetches midterm election data from PredictIt."""

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

    async def fetch_election_markets(self) -> list[dict]:
        """Fetch all markets and filter for elections."""
        session = await self._get_session()
        try:
            async with session.get(PREDICTIT_API, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    logger.error(f"PredictIt API error: {resp.status}")
                    return []
                data = await resp.json()
                markets = data.get("markets", [])
                return self._normalize_markets(markets)
        except Exception as e:
            logger.error(f"PredictIt fetch error: {e}")
            return []

    def _normalize_markets(self, markets: list[dict]) -> list[dict]:
        normalized = []
        keywords = [
            "senate", "house", "governor", "midterm", "congress", "election",
            "republican", "democrat", "control", "majority", "2026", "seat"
        ]

        for m in markets:
            name = (m.get("name") or m.get("shortName") or "").lower()
            if not any(kw in name for kw in keywords):
                continue

            race_type = "other"
            if "senate" in name:
                race_type = "senate"
            elif "house" in name or "representative" in name:
                race_type = "house"
            elif "governor" in name:
                race_type = "governor"
            elif "control" in name or "majority" in name:
                race_type = "control"

            state = self._extract_state(m.get("name", ""))

            contracts = m.get("contracts", [])
            outcomes = []
            for c in contracts:
                outcomes.append({
                    "name": c.get("name", ""),
                    "probability": c.get("lastTradePrice"),
                    "token_id": str(c.get("id", "")),
                    "best_buy_yes": c.get("bestBuyYesCost"),
                    "best_buy_no": c.get("bestBuyNoCost"),
                })

            normalized.append({
                "source": "predictit",
                "source_id": str(m.get("id", "")),
                "event_id": str(m.get("id", "")),
                "title": m.get("name", ""),
                "event_title": m.get("name", ""),
                "slug": m.get("url", "").split("/")[-1] if m.get("url") else "",
                "race_type": race_type,
                "state": state,
                "outcomes": outcomes,
                "volume": 0,  # PredictIt doesn't expose volume
                "liquidity": 0,
                "active": m.get("status") == "Open",
                "closed": m.get("status") == "Closed",
                "end_date": m.get("dateEnd"),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })

        return normalized

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
        for name, abbr in states.items():
            if name.lower() in title.lower() or f" {abbr} " in f" {title} ":
                return abbr
        return None
