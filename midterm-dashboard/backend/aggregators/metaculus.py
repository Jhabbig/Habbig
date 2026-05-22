"""Metaculus aggregator.

Metaculus (https://www.metaculus.com) is a public forecasting platform with a
free read-only API. The community prediction (median of forecasters) is a
useful third-party probability signal alongside market prices.

API reference: https://www.metaculus.com/api2/
"""

from __future__ import annotations

import aiohttp
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from data_sources.countries import COUNTRIES, COUNTRY_ADJECTIVES
from data_sources.fips import STATE_NAMES, STATE_FIPS

logger = logging.getLogger(__name__)

METACULUS_API = "https://www.metaculus.com/api2/questions/"

# Search terms that yield US midterm + international election questions.
SEARCH_TERMS = [
    "2026 midterm",
    "2026 senate",
    "2026 house",
    "2026 governor",
    "2026 election",
    "presidential election",
]


class MetaculusAggregator:
    """Fetches binary forecasting questions from Metaculus."""

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
        questions = await self._search()
        return self._normalize(questions, world=False)

    async def fetch_world_election_markets(self) -> list[dict]:
        questions = await self._search()
        return self._normalize(questions, world=True)

    async def _search(self) -> list[dict]:
        session = await self._get_session()
        seen: dict[int, dict] = {}
        for term in SEARCH_TERMS:
            try:
                params = {
                    "search": term,
                    "status": "open",
                    "type": "forecast",
                    "limit": 50,
                    "include_description": "false",
                }
                async with session.get(METACULUS_API, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.info(f"Metaculus search '{term}' returned {resp.status}")
                        continue
                    data = await resp.json()
                    for q in data.get("results", []):
                        qid = q.get("id")
                        if qid and qid not in seen:
                            seen[qid] = q
            except Exception as e:
                logger.warning(f"Metaculus search '{term}' error: {e}")
                continue
        logger.info(f"Metaculus fetched {len(seen)} unique questions across {len(SEARCH_TERMS)} searches")
        return list(seen.values())

    def _normalize(self, questions: list[dict], *, world: bool) -> list[dict]:
        normalized = []
        for q in questions:
            title = q.get("title") or q.get("title_short") or ""
            text = title.lower()
            if not any(kw in text for kw in ("election", "senate", "house", "governor", "president", "prime minister", "midterm")):
                continue

            race_type = self._race_type(text)
            state = self._extract_state(title)
            country = self._extract_country(title)
            is_world = race_type == "world" or (country is not None and not state)
            if world and not is_world:
                continue
            if not world and is_world:
                continue

            outcomes = self._outcomes(q)
            if not outcomes:
                continue

            close_time = q.get("close_time") or q.get("scheduled_close_time")
            try:
                end_iso = datetime.fromisoformat(close_time.replace("Z", "+00:00")).isoformat() if close_time else None
            except (ValueError, AttributeError):
                end_iso = None

            normalized.append({
                "source": "metaculus",
                "source_id": str(q.get("id", "")),
                "event_id": str(q.get("id", "")),
                "title": title,
                "event_title": title,
                "slug": q.get("page_url", "").rstrip("/").split("/")[-1] if q.get("page_url") else "",
                "race_type": "world" if is_world else (race_type or "other"),
                "state": country if is_world else state,
                "outcomes": outcomes,
                "volume": float(q.get("number_of_forecasts", 0) or 0),  # proxy
                "liquidity": 0.0,
                "active": q.get("status") == "open",
                "closed": q.get("status") in {"closed", "resolved"},
                "end_date": end_iso,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })
        return normalized

    @staticmethod
    def _outcomes(q: dict) -> list[dict]:
        """Pull the community prediction off a Metaculus question.

        Binary questions expose ``community_prediction.full.q2`` (median).
        Multiple-choice questions expose per-option ``best_estimate``.
        """
        if q.get("possibilities", {}).get("type") == "binary" or q.get("type") == "forecast":
            cp = q.get("community_prediction") or {}
            full = cp.get("full") if isinstance(cp, dict) else None
            median = None
            if isinstance(full, dict):
                median = full.get("q2")
            if median is None:
                return []
            try:
                p = float(median)
            except (ValueError, TypeError):
                return []
            return [
                {"name": "Yes", "probability": p, "token_id": None},
                {"name": "No", "probability": 1.0 - p, "token_id": None},
            ]
        return []

    @staticmethod
    def _race_type(text: str) -> Optional[str]:
        if "senate" in text:
            return "senate"
        if "house" in text or "representative" in text:
            return "house"
        if "governor" in text:
            return "governor"
        if "president" in text and ("us " in text or "united states" in text or "american" in text):
            return "presidential"
        if "president" in text or "prime minister" in text or "chancellor" in text:
            return "world"
        return None

    @staticmethod
    def _extract_state(title: str) -> Optional[str]:
        if not title:
            return None
        tl = title.lower()
        is_dc = "washington d.c." in tl or "washington, d.c." in tl
        sorted_states = sorted(STATE_NAMES.items(), key=lambda kv: len(kv[1]), reverse=True)
        for abbr, name in sorted_states:
            n = name.lower()
            if n == "washington" and is_dc:
                continue
            if re.search(rf"\b{re.escape(n)}\b", tl):
                return abbr
        ambiguous = {"IN", "OR", "ME", "OH", "AL", "OK", "HI", "ID", "PA", "MA", "AK", "AR", "DE"}
        padded = f" {title} "
        for abbr in STATE_FIPS:
            if abbr in ambiguous:
                continue
            if f" {abbr} " in padded:
                return abbr
        return None

    @staticmethod
    def _extract_country(title: str) -> Optional[str]:
        if not title:
            return None
        tl = title.lower()
        sorted_countries = sorted(COUNTRIES.items(), key=lambda kv: len(kv[1][1]), reverse=True)
        for code, (_iso3, name) in sorted_countries:
            if name.lower() in tl:
                return code
        for code, adj in COUNTRY_ADJECTIVES.items():
            if re.search(rf"\b{re.escape(adj.lower())}\b", tl):
                return code
        return None
