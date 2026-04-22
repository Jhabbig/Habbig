"""FiveThirtyEight (ABC News) election-probability scraper.

No public API — 538's election pages are Next.js apps that embed a
large JSON payload in a ``<script id="__NEXT_DATA__">`` tag. We fetch
the HTML, extract the JSON, and walk it looking for the latest
forecast percentage per candidate / outcome.

Structure changes occasionally; parse defensively and surface a
ProviderError so the sync job logs + moves on. The 6-hour in-memory
cache means we only hit the page four times a day at worst — even
with market-by-market queries the fetcher is a single page load.

Scope: US politics only (presidential, senate, house). The matcher
won't even consider 538 candidates for other categories because 538
doesn't publish forecasts outside of US elections.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import httpx

from external_forecasts.base import Candidate, clamp_probability


log = logging.getLogger("forecasts.fivethirtyeight")
_TIMEOUT = 15.0
_CACHE_TTL_SECONDS = 6 * 3600

# Pages worth scraping. The landing page aggregates into election pages.
# We don't follow links dynamically — any year-specific pages go here.
_SOURCE_PAGES: tuple[str, ...] = (
    "https://projects.fivethirtyeight.com/polls/president-general/2024/",
    "https://projects.fivethirtyeight.com/2024-election-forecast/",
)

# Only consider candidates for markets in these categories.
_SUPPORTED_CATEGORIES: frozenset[str] = frozenset({
    "politics", "us_politics", "elections", "election",
})

# Module-level cache. (url -> (fetched_at, list[Candidate]))
_cache: dict[str, tuple[float, list[Candidate]]] = {}


async def fetch_matching(market: dict) -> list[Candidate]:
    """Return FiveThirtyEight candidates that plausibly map to our
    market. Only US politics is in scope — non-political markets get
    [] back immediately."""
    category = str(market.get("category") or "").lower()
    if category not in _SUPPORTED_CATEGORIES:
        return []

    all_candidates: list[Candidate] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for url in _SOURCE_PAGES:
            try:
                candidates = await _fetch_page_candidates(client, url)
            except Exception as exc:  # noqa: BLE001 — never crash the sync
                log.warning("fivethirtyeight %s: %s", url, exc)
                continue
            all_candidates.extend(candidates)

    return all_candidates[:16]  # safety cap for the matcher prompt size


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

    payload = _extract_next_data(html)
    if payload is None:
        # Page rendered without Next.js or structure changed.
        _cache[url] = (now, [])
        return []

    candidates = list(_walk_for_probabilities(payload, url))
    _cache[url] = (now, candidates)
    return candidates


# ── Parsing helpers ──────────────────────────────────────────────────


_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


def _extract_next_data(html: str) -> Optional[dict]:
    """Pull the Next.js JSON payload out of a page. Returns None if
    the script tag isn't present."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


def _walk_for_probabilities(payload, source_url: str):
    """Yield ``Candidate`` records from any dict-shaped node that looks
    like ``{"candidate": ..., "probability": ...}`` or the analogous
    ``{"state": ..., "win_prob": ...}``. The structure varies, so we
    duck-type instead of hard-coding a path."""
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

    # Stable id: combine the source page + the candidate / state name.
    key = f"{source_url}#{name}".lower()
    return Candidate(
        provider="fivethirtyeight",
        provider_market_id=key,
        question=f"538 forecast: {name}",
        probability=prob,
        close_at=None,
        resolved=False,
        url=source_url,
    )
