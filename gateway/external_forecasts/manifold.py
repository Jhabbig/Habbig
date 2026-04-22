"""Manifold Markets adapter.

Public API, no auth required:
  https://api.manifold.markets/v0/search-markets?term=<query>

Manifold returns markets of several types. We only care about ``BINARY``
(YES/NO). The current probability lives in the top-level ``probability``
field. For markets that have already resolved, ``isResolved`` is true
and ``resolution`` is either "YES" or "NO" — the sync job ignores
resolved candidates before scoring.

Heuristic: Manifold is noisier than Metaculus (anyone can create a
market), so the matcher's confidence on Manifold candidates tends to
run lower. That's fine — ``list_low_confidence_equivalences`` will
surface the weak ones to admin review.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from external_forecasts.base import Candidate, clamp_probability


log = logging.getLogger("forecasts.manifold")
_BASE = "https://api.manifold.markets/v0"
_TIMEOUT = 10.0
_MAX_CANDIDATES = 8


async def fetch_matching(market: dict) -> list[Candidate]:
    """Return up to ``_MAX_CANDIDATES`` Manifold binary markets that
    search-match our market's question text. Never raises."""
    query = _search_query(market)
    if not query:
        return []

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            r = await client.get(
                f"{_BASE}/search-markets",
                params={"term": query, "limit": 20},
                headers={"User-Agent": "narve.ai forecast-benchmark (support@narve.ai)"},
            )
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("manifold search failed for %r: %s", query, exc)
            return []

    results = data if isinstance(data, list) else (data or {}).get("markets") or []
    out: list[Candidate] = []
    for m in results:
        cand = _parse_market(m)
        if cand is None:
            continue
        out.append(cand)
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


def _search_query(market: dict) -> str:
    q = str(market.get("market_question") or market.get("question") or "").strip()
    if not q:
        return ""
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+", q) if len(t) > 3]
    tokens.sort(key=len, reverse=True)
    return " ".join(tokens[:8]) if tokens else q[:80]


def _parse_market(m: dict) -> Optional[Candidate]:
    if m.get("outcomeType") != "BINARY":
        return None
    prob_raw = m.get("probability")
    if prob_raw is None:
        return None
    try:
        prob = clamp_probability(prob_raw)
    except ValueError:
        return None

    mid = m.get("id") or m.get("slug")
    if not mid:
        return None

    # closeTime is ms since epoch in Manifold's schema.
    close_at_ms = m.get("closeTime")
    close_at: Optional[int] = int(close_at_ms / 1000) if isinstance(close_at_ms, (int, float)) else None

    return Candidate(
        provider="manifold",
        provider_market_id=str(mid),
        question=str(m.get("question") or "").strip(),
        probability=prob,
        close_at=close_at,
        resolved=bool(m.get("isResolved")),
        url=m.get("url") or (f"https://manifold.markets/market/{m.get('slug')}" if m.get("slug") else None),
        volume=float(m.get("volume") or 0.0) or None,
    )
