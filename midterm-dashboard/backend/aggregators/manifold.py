from __future__ import annotations
"""Manifold Markets aggregator.

Manifold (manifold.markets) is a free, public-API play-money prediction
market that often has US-political markets with high participation. The
v0 API is open — no API key required for read access.

Endpoint: ``GET https://api.manifold.markets/v0/search-markets`` lets us
text-search; ``/v0/markets`` does paginated listing. We search the relevant
keywords and filter to 2026 US-election markets.

Two market types to handle:
  - ``BINARY``     → straight Yes/No, probability is the YES side
  - ``MULTIPLE_CHOICE`` (and the legacy ``MULTI_NUMERIC``) → list of
    ``answers[]`` each with their own probability

Both normalize into our standard ``outcomes`` schema so the cross-source
matcher doesn't have to know which provider a market came from.
"""

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

from ._retry import fetch_json_with_retry

logger = logging.getLogger(__name__)

MANIFOLD_API = "https://api.manifold.markets/v0"

_SEARCH_TERMS = [
    "2026 senate", "2026 governor", "2026 house",
    "2026 midterm", "2026 congress",
    "midterms 2026",
]

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
    """Find the 2-letter state code from a market title or description.

    Same approach as Polymarket._extract_state — match full names with word
    boundaries first, then fall back to unambiguous abbreviations. We
    explicitly skip "Washington" → WA when the title mentions D.C., which is
    the common false positive (Manifold has lots of DC-related markets).
    """
    if not text:
        return None
    text_lower = text.lower()
    is_dc = "washington d.c." in text_lower or "washington, d.c." in text_lower or " d.c." in text_lower
    for name, abbr in _STATES.items():
        if name.lower() == "washington" and is_dc:
            continue
        if re.search(rf"\b{re.escape(name.lower())}\b", text_lower):
            return abbr
    # Conservative abbreviation fallback — only the non-ambiguous codes
    safe_abbrs = {abbr for name, abbr in _STATES.items()
                  if abbr not in {"IN", "OR", "ME", "OH", "AL", "OK", "HI", "ID", "PA", "MA"}}
    for name, abbr in _STATES.items():
        if abbr in safe_abbrs and f" {abbr} " in f" {text} ":
            return abbr
    return None


def _classify_race_type(title: str) -> str:
    title_lower = (title or "").lower()
    # "control"/"majority" markets often also mention senate/house — check
    # control FIRST so a "who controls the Senate" market is tagged as
    # control, not as the per-state senate race.
    if "control" in title_lower or "majority" in title_lower or "flip" in title_lower:
        return "control"
    if "senate" in title_lower:
        return "senate"
    if "house" in title_lower or "representative" in title_lower:
        return "house"
    if "governor" in title_lower or "gubernatorial" in title_lower:
        return "governor"
    return "other"


def _is_open(market: dict, max_years_out: float = 3.0) -> bool:
    """Filter out resolved/expired markets. Manifold returns ``isResolved``
    and ``closeTime`` (ms-since-epoch)."""
    if market.get("isResolved"):
        return False
    close_ms = market.get("closeTime")
    if not close_ms:
        return True  # open-ended; include
    try:
        close_dt = datetime.fromtimestamp(close_ms / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return True
    now = datetime.now(timezone.utc)
    if close_dt < now:
        return False
    if close_dt > now + timedelta(days=365 * max_years_out):
        return False
    return True


class ManifoldAggregator:
    """Fetches US 2026 midterm-election markets from Manifold."""

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

    async def _search(self, term: str, limit: int = 100) -> list[dict]:
        """Manifold search-markets endpoint. Returns a flat list of markets."""
        session = await self._get_session()
        params = {"term": term, "limit": limit}
        data = await fetch_json_with_retry(
            session, f"{MANIFOLD_API}/search-markets",
            params=params, timeout=15, source_label="manifold-search",
        )
        return data if isinstance(data, list) else []

    async def fetch_election_markets(self) -> list[dict]:
        """Search several relevant terms, dedupe by market id, normalize."""
        seen: dict[str, dict] = {}
        for term in _SEARCH_TERMS:
            markets = await self._search(term)
            for m in markets:
                mid = m.get("id")
                if mid and mid not in seen:
                    seen[mid] = m
        logger.info(f"Manifold: pulled {len(seen)} unique election-keyword markets")
        return self._normalize(list(seen.values()))

    def _normalize(self, markets: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        for m in markets:
            if not _is_open(m):
                continue
            question = (m.get("question") or "").strip()
            description = (m.get("textDescription") or m.get("description") or "")
            if isinstance(description, dict):
                # Manifold sometimes returns description as TipTap JSON
                description = ""
            combined = f"{question} {description}".lower()

            # Filter for actual midterm relevance — search-markets is fuzzy
            # so we re-check the title for 2026 + a race-type keyword.
            if "2026" not in combined:
                continue
            race_type = _classify_race_type(question)
            if race_type == "other":
                continue

            state = _extract_state(question) or _extract_state(description if isinstance(description, str) else "")

            outcome_type = (m.get("outcomeType") or "").upper()
            outcomes: list[dict] = []

            if outcome_type == "BINARY":
                yes_prob = m.get("probability")
                if yes_prob is None:
                    continue
                try:
                    p = float(yes_prob)
                except (TypeError, ValueError):
                    continue
                outcomes = [
                    {"name": "Yes", "probability": p, "token_id": m.get("id")},
                    {"name": "No",  "probability": max(0.0, 1.0 - p), "token_id": None},
                ]
            elif outcome_type in ("MULTIPLE_CHOICE", "FREE_RESPONSE", "MULTI_NUMERIC", "POLL"):
                answers = m.get("answers") or []
                for a in answers:
                    if not isinstance(a, dict):
                        continue
                    try:
                        p = float(a.get("probability"))
                    except (TypeError, ValueError):
                        continue
                    outcomes.append({
                        "name": (a.get("text") or "").strip(),
                        "probability": p,
                        "token_id": a.get("id"),
                    })
            else:
                # PSEUDO_NUMERIC, NUMERIC, BOUNTIED_QUESTION etc. — not
                # something we can map to our market schema.
                continue

            if not outcomes:
                continue

            close_iso = None
            close_ms = m.get("closeTime")
            if close_ms:
                try:
                    close_iso = datetime.fromtimestamp(close_ms / 1000.0, tz=timezone.utc).isoformat()
                except (TypeError, ValueError, OSError):
                    close_iso = None

            normalized.append({
                "source": "manifold",
                "source_id": str(m.get("id", "")),
                "event_id": str(m.get("id", "")),
                "title": question,
                "event_title": question,
                "slug": (m.get("url") or "").rsplit("/", 1)[-1],
                "race_type": race_type,
                "state": state,
                "outcomes": outcomes,
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("totalLiquidity", 0) or 0),
                "active": True,
                "closed": False,
                "end_date": close_iso,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            })
        logger.info(f"Manifold: normalized {len(normalized)} active 2026 election markets")
        return normalized
