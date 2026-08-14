"""Manifold Markets aggregator.

Manifold (https://manifold.markets) is a play-money prediction market with a
free public API. We pull the ``elections`` and ``politics`` group markets and
filter to ones related to the 2026 US midterms or international elections.
Output is normalized to the same shape as the Polymarket / Kalshi feeds so
the rest of the pipeline doesn't need to know about Manifold-specifics.

API reference: https://docs.manifold.markets/api
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

MANIFOLD_API = "https://api.manifold.markets/v0"

# Group slugs that reliably contain election markets. Manifold tags markets by
# group; pulling group-by-group is much cheaper and more accurate than walking
# the whole catalogue.
ELECTION_GROUP_SLUGS = [
    "us-politics",
    "elections",
    "2026-elections",
    "2026-midterms",
    "us-senate",
    "us-house",
    "us-governors",
    "international-elections",
]


class ManifoldAggregator:
    """Fetches election markets from Manifold."""

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
        markets = await self._fetch_groups()
        return self._normalize(markets, world=False)

    async def fetch_world_election_markets(self) -> list[dict]:
        markets = await self._fetch_groups()
        return self._normalize(markets, world=True)

    async def _fetch_groups(self) -> list[dict]:
        session = await self._get_session()
        seen: dict[str, dict] = {}
        for slug in ELECTION_GROUP_SLUGS:
            try:
                url = f"{MANIFOLD_API}/group/{slug}/markets"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 404:
                        # Group renamed or absent — skip
                        continue
                    if resp.status != 200:
                        logger.info(f"Manifold group {slug} returned {resp.status}")
                        continue
                    data = await resp.json()
                    if not isinstance(data, list):
                        continue
                    for m in data:
                        mid = m.get("id")
                        if mid and mid not in seen:
                            seen[mid] = m
            except Exception as e:
                logger.warning(f"Manifold group {slug} fetch error: {e}")
                continue
        logger.info(f"Manifold fetched {len(seen)} unique markets across {len(ELECTION_GROUP_SLUGS)} groups")
        return list(seen.values())

    def _normalize(self, markets: list[dict], *, world: bool) -> list[dict]:
        normalized = []
        for m in markets:
            if m.get("isResolved"):
                continue
            if m.get("closeTime"):
                # closeTime is unix-ms
                close_dt = datetime.fromtimestamp(m["closeTime"] / 1000.0, tz=timezone.utc)
                if close_dt < datetime.now(timezone.utc):
                    continue
            else:
                close_dt = None

            question = m.get("question") or ""
            text = question.lower()
            race_type = self._race_type(text)
            state = self._extract_state(question)
            country = self._extract_country(question)

            is_world = race_type == "world" or (country is not None and not state)
            if world and not is_world:
                continue
            if not world and is_world:
                continue

            outcomes = self._outcomes(m)
            if not outcomes:
                continue

            normalized.append({
                "source": "manifold",
                "source_id": m.get("id", ""),
                "event_id": m.get("id", ""),
                "title": question,
                "event_title": question,
                "slug": m.get("slug", ""),
                "race_type": "world" if is_world else (race_type or "other"),
                "state": country if is_world else state,
                "outcomes": outcomes,
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("totalLiquidity", 0) or 0),
                "active": not m.get("isResolved", False),
                "closed": bool(m.get("isResolved", False)),
                "end_date": close_dt.isoformat() if close_dt else None,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })
        return normalized

    @staticmethod
    def _outcomes(market: dict) -> list[dict]:
        """Extract probability-bearing outcomes from a Manifold market.

        Binary markets have ``probability`` (Yes vs No). MultipleChoice have
        ``answers`` with per-answer probabilities.
        """
        outcome_type = market.get("outcomeType")
        if outcome_type == "BINARY":
            p = market.get("probability")
            if p is None:
                return []
            return [
                {"name": "Yes", "probability": float(p), "token_id": None},
                {"name": "No", "probability": 1.0 - float(p), "token_id": None},
            ]
        if outcome_type in ("MULTIPLE_CHOICE", "FREE_RESPONSE"):
            answers = market.get("answers") or []
            out = []
            for a in answers:
                p = a.get("probability")
                if p is None:
                    continue
                out.append({
                    "name": a.get("text") or a.get("answer") or "",
                    "probability": float(p),
                    "token_id": a.get("id"),
                })
            return out
        return []

    @staticmethod
    def _race_type(text: str) -> Optional[str]:
        if "senate" in text:
            return "senate"
        if "house" in text or "representative" in text:
            return "house"
        if "governor" in text:
            return "governor"
        if "president" in text and ("us " in text or "united states" in text or "trump" in text):
            return "presidential"
        if "president" in text or "prime minister" in text or "chancellor" in text:
            return "world"
        if "control" in text or "majority" in text:
            return "control"
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
