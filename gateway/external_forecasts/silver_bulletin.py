"""Silver Bulletin (Nate Silver's Substack) scraper.

Nate Silver's forecasts moved off 538 to silverbulletin.com. The
election pages are also Next.js and use the same ``__NEXT_DATA__``
embedding pattern as 538 — so this adapter is 95% the same as
``fivethirtyeight.py``, just with different source URLs. We keep it
in its own file so:
  - a future API from Silver Bulletin can be swapped in without
    touching 538's code, and
  - the admin UI can disable one provider independently.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import httpx

from external_forecasts.base import Candidate, clamp_probability


log = logging.getLogger("forecasts.silver_bulletin")
_TIMEOUT = 15.0
_CACHE_TTL_SECONDS = 6 * 3600

_SOURCE_PAGES: tuple[str, ...] = (
    "https://www.natesilver.net/p/nate-silver-2024-president-election-polls-model",
)

_SUPPORTED_CATEGORIES: frozenset[str] = frozenset({
    "politics", "us_politics", "elections", "election",
})

_cache: dict[str, tuple[float, list[Candidate]]] = {}

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


async def fetch_matching(market: dict) -> list[Candidate]:
    category = str(market.get("category") or "").lower()
    if category not in _SUPPORTED_CATEGORIES:
        return []

    out: list[Candidate] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for url in _SOURCE_PAGES:
            try:
                out.extend(await _fetch_page_candidates(client, url))
            except Exception as exc:  # noqa: BLE001
                log.warning("silver_bulletin %s: %s", url, exc)
                continue
    return out[:16]


async def _fetch_page_candidates(client: httpx.AsyncClient, url: str) -> list[Candidate]:
    now = time.time()
    cached = _cache.get(url)
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    r = await client.get(
        url,
        headers={"User-Agent": "narve.ai forecast-benchmark (support@narve.ai)"},
    )
    r.raise_for_status()
    html = r.text

    m = _NEXT_DATA_RE.search(html)
    if not m:
        _cache[url] = (now, [])
        return []
    try:
        payload = json.loads(m.group(1))
    except (ValueError, TypeError):
        _cache[url] = (now, [])
        return []

    candidates = list(_walk(payload, url))
    _cache[url] = (now, candidates)
    return candidates


def _walk(payload, source_url: str):
    stack = [payload]
    seen = 0
    while stack and seen < 200:
        node = stack.pop()
        if isinstance(node, dict):
            cand = _dict_to_candidate(node, source_url)
            if cand is not None:
                seen += 1
                yield cand
            for v in node.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, (dict, list)):
                    stack.append(v)


def _dict_to_candidate(node: dict, source_url: str) -> Optional[Candidate]:
    prob_raw = (
        node.get("probability")
        or node.get("win_prob")
        or node.get("winprob")
        or node.get("prob")
    )
    if prob_raw is None:
        return None
    name = (
        node.get("candidate")
        or node.get("name")
        or node.get("state")
        or node.get("question")
    )
    if not name:
        return None
    try:
        prob = clamp_probability(prob_raw)
    except ValueError:
        return None
    key = f"{source_url}#{name}".lower()
    return Candidate(
        provider="silver_bulletin",
        provider_market_id=key,
        question=f"Silver Bulletin: {name}",
        probability=prob,
        close_at=None,
        resolved=False,
        url=source_url,
    )
