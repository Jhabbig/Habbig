from __future__ import annotations
"""Metaculus aggregator.

Metaculus (metaculus.com) is a forecasting-tournament platform — community
predictions on long-horizon questions, with a strong calibration record on
US politics. Different shape from prediction markets (no money flowing, no
volume; instead each question has a ``community_prediction`` aggregating
all individual forecasts).

We treat the Metaculus community median as a "source" comparable to the
prediction markets — useful because Metaculus tends to over/under-shoot
in known ways relative to play-money markets like Manifold, so the
divergence is informative.

API: ``GET https://www.metaculus.com/api2/questions/?topic=politics``
returns paginated questions. Each ``question`` has a ``possibilities``
dict telling us if it's binary; the ``community_prediction.full.q2`` is
the median community probability of the YES outcome. Open access, no
API key needed for read.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from ._retry import fetch_json_with_retry

logger = logging.getLogger(__name__)

METACULUS_API = "https://www.metaculus.com/api2/questions/"


_STATES = {
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
    "Wisconsin": "WI", "Wyoming": "WY",
}


def _extract_state(text: str) -> Optional[str]:
    if not text:
        return None
    text_lower = text.lower()
    is_dc = "washington d.c." in text_lower or "washington, d.c." in text_lower or " d.c." in text_lower
    for name, abbr in _STATES.items():
        if name.lower() == "washington" and is_dc:
            continue
        if re.search(rf"\b{re.escape(name.lower())}\b", text_lower):
            return abbr
    return None


def _classify_race_type(title: str) -> str:
    t = (title or "").lower()
    # See manifold._classify_race_type — control/majority/flip first so
    # "who controls the Senate?" doesn't get tagged as a per-state senate race.
    if "control" in t or "majority" in t or "flip" in t:
        return "control"
    if "senate" in t:
        return "senate"
    if "house" in t or "representative" in t:
        return "house"
    if "governor" in t or "gubernatorial" in t:
        return "governor"
    return "other"


def _community_yes_prob(q: dict) -> Optional[float]:
    """Pull the community median YES probability from a Metaculus question.

    Schema is gnarly because Metaculus has changed it over time. The most
    reliable spot is ``community_prediction.full.q2`` (median). Fall back
    to ``aggregations.recency_weighted.latest.centers[0]`` which the new
    API uses for binary questions.
    """
    cp = q.get("community_prediction") or {}
    full = cp.get("full") or {}
    q2 = full.get("q2")
    if isinstance(q2, (int, float)) and 0.0 <= q2 <= 1.0:
        return float(q2)

    aggs = q.get("aggregations") or {}
    rw = aggs.get("recency_weighted") or {}
    latest = rw.get("latest") or {}
    centers = latest.get("centers") or []
    if centers and isinstance(centers[0], (int, float)) and 0.0 <= centers[0] <= 1.0:
        return float(centers[0])
    return None


class MetaculusAggregator:
    """Fetches US 2026 midterm forecasting questions from Metaculus."""

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _fetch_page(self, offset: int = 0) -> dict | None:
        session = await self._get_session()
        params = {
            "topic": "politics",
            "status": "open",
            "limit": 50,
            "offset": offset,
            "order_by": "-publish_time",
        }
        return await fetch_json_with_retry(
            session, METACULUS_API, params=params, timeout=20,
            source_label="metaculus",
        )

    async def fetch_election_markets(self) -> list[dict]:
        all_questions: list[dict] = []
        # Cap the walk — Metaculus has a long tail of unrelated politics
        # questions and we only want the 2026 ones.
        for offset in (0, 50, 100):
            page = await self._fetch_page(offset=offset)
            if not isinstance(page, dict):
                break
            results = page.get("results") or []
            if not results:
                break
            all_questions.extend(results)
            if not page.get("next"):
                break
        logger.info(f"Metaculus: fetched {len(all_questions)} politics questions")
        return self._normalize(all_questions)

    def _normalize(self, questions: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        for q in questions:
            title = (q.get("title") or q.get("question_title") or "").strip()
            if not title:
                continue
            combined = f"{title} {(q.get('description') or '')}".lower()
            # Filter to 2026 midterm-relevance — Metaculus politics topic
            # includes long-running questions (e.g. 2028 presidential).
            if "2026" not in combined:
                continue
            race_type = _classify_race_type(title)
            if race_type == "other":
                continue

            # Only binary questions map cleanly — Metaculus has numeric
            # range questions too which don't fit our schema.
            possibilities = q.get("possibilities") or {}
            q_type = possibilities.get("type") or q.get("type")
            if q_type and q_type not in ("binary", "BINARY"):
                continue

            yes_prob = _community_yes_prob(q)
            if yes_prob is None:
                continue

            state = _extract_state(title)

            qid = q.get("id")
            slug = q.get("page_url") or f"/questions/{qid}/"
            close_time = q.get("close_time") or q.get("resolve_time")
            normalized.append({
                "source": "metaculus",
                "source_id": str(qid or ""),
                "event_id": str(qid or ""),
                "title": title,
                "event_title": title,
                "slug": slug.lstrip("/"),
                "race_type": race_type,
                "state": state,
                "outcomes": [
                    {"name": "Yes", "probability": yes_prob, "token_id": str(qid)},
                    {"name": "No",  "probability": max(0.0, 1.0 - yes_prob), "token_id": None},
                ],
                "volume": 0.0,       # Metaculus has no money / no volume
                "liquidity": 0.0,
                "active": True,
                "closed": False,
                "end_date": close_time,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })
        logger.info(f"Metaculus: normalized {len(normalized)} 2026 election questions")
        return normalized
