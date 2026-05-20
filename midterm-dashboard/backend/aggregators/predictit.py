import aiohttp
import logging
from datetime import datetime, timezone
from typing import Optional

from data_sources.fips import STATE_NAMES, STATE_FIPS

logger = logging.getLogger(__name__)

PREDICTIT_API = "https://www.predictit.org/api/marketdata/all/"


class PredictItAggregator:
    """Fetches midterm election data from PredictIt.

    PredictIt's CFTC no-action letter has expired and the platform has been
    winding down — the public marketdata endpoint may return empty or 404.
    The aggregator logs a warning and yields an empty list so the rest of
    the dashboard keeps working.
    """

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
                    logger.info(f"PredictIt returned {resp.status} (likely shutdown)")
                    return []
                data = await resp.json()
                markets = data.get("markets", [])
                return self._normalize_markets(markets)
        except Exception as e:
            logger.warning(f"PredictIt fetch error: {e}")
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
        if not title:
            return None
        title_lower = title.lower()
        # Full names first; abbreviations only for codes that don't collide
        # with English words.
        for abbr, name in STATE_NAMES.items():
            if name.lower() in title_lower:
                return abbr
        ambiguous_abbrs = {"IN", "OR", "ME", "OH", "AL", "OK", "HI", "ID", "PA", "MA", "AK", "AR", "DE"}
        padded = f" {title} "
        for abbr in STATE_FIPS:
            if abbr in ambiguous_abbrs:
                continue
            if f" {abbr} " in padded:
                return abbr
        return None
