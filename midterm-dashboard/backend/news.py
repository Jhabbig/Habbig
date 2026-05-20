from __future__ import annotations
"""News article fetching for movement explanations.

Two sources, tried in order:

1. NewsAPI.org — primary, requires NEWS_API_KEY. Cleaner results, reliable
   publish timestamps, full-text snippets.
2. GDELT 2.0 doc API — free fallback, no API key. Broader corpus but
   sparser metadata.

Both return a normalized ``Article`` dict so the LLM analyzer doesn't need
to know which source produced it. Articles are filtered to those published
inside (or before the end of) the requested time window — anything emitted
after the price-move window can't be causal, so we exclude it server-side
before the data ever reaches Claude.
"""

import logging
import os
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

from aggregators._retry import fetch_json_with_retry

logger = logging.getLogger(__name__)

NEWSAPI_URL = "https://newsapi.org/v2/everything"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def _iso_to_dt(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _normalize_newsapi(item: dict) -> dict:
    source = (item.get("source") or {}).get("name", "") if isinstance(item.get("source"), dict) else ""
    return {
        "headline": (item.get("title") or "").strip(),
        "snippet": (item.get("description") or item.get("content") or "")[:600].strip(),
        "url": item.get("url") or "",
        "source": source or "",
        "published_at": item.get("publishedAt") or "",
        "provider": "newsapi",
    }


def _normalize_gdelt(item: dict) -> dict:
    raw_ts = item.get("seendate") or ""
    iso = ""
    if raw_ts and len(raw_ts) >= 14:
        try:
            iso = (
                f"{raw_ts[0:4]}-{raw_ts[4:6]}-{raw_ts[6:8]}T"
                f"{raw_ts[9:11]}:{raw_ts[11:13]}:{raw_ts[13:15]}Z"
            )
        except (IndexError, ValueError):
            iso = ""
    return {
        "headline": (item.get("title") or "").strip(),
        "snippet": (item.get("title") or "")[:600].strip(),
        "url": item.get("url") or "",
        "source": (item.get("domain") or "").strip(),
        "published_at": iso,
        "provider": "gdelt",
    }


async def _fetch_newsapi(
    session: aiohttp.ClientSession,
    query: str,
    from_ts: datetime,
    to_ts: datetime,
    limit: int,
) -> list[dict]:
    api_key = os.getenv("NEWS_API_KEY", "").strip()
    if not api_key:
        return []
    params = {
        "q": query,
        "from": from_ts.strftime("%Y-%m-%dT%H:%M:%S"),
        "to": to_ts.strftime("%Y-%m-%dT%H:%M:%S"),
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": min(max(1, limit), 50),
        "apiKey": api_key,
    }
    data = await fetch_json_with_retry(
        session, NEWSAPI_URL, params=params, timeout=15,
        source_label="newsapi", max_attempts=2,
    )
    if not data or data.get("status") != "ok":
        if data and data.get("status") == "error":
            logger.warning(f"NewsAPI error: {data.get('code')} {data.get('message')}")
        return []
    return [_normalize_newsapi(a) for a in (data.get("articles") or [])]


async def _fetch_gdelt(
    session: aiohttp.ClientSession,
    query: str,
    from_ts: datetime,
    to_ts: datetime,
    limit: int,
) -> list[dict]:
    """GDELT 2.0 doc API. Free, no key required.

    Uses ``startdatetime`` / ``enddatetime`` in YYYYMMDDHHMMSS format and
    returns JSON when ``format=json`` is set. Note: GDELT's HTTP fronted
    can be slow and occasionally returns HTML on errors — we treat any
    non-JSON response as no-results.
    """
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": min(max(1, limit), 50),
        "format": "json",
        "sort": "datedesc",
        "startdatetime": from_ts.strftime("%Y%m%d%H%M%S"),
        "enddatetime": to_ts.strftime("%Y%m%d%H%M%S"),
    }
    url = f"{GDELT_URL}?{urllib.parse.urlencode(params, safe=':')}"
    data = await fetch_json_with_retry(
        session, url, timeout=20, source_label="gdelt", max_attempts=2,
    )
    if not isinstance(data, dict):
        return []
    return [_normalize_gdelt(a) for a in (data.get("articles") or [])]


def _build_query(race_type: str, state: str, race_context: dict | None) -> str:
    """Build a news search query for a race.

    Combines:
      - The state's full name (more reliable than the abbreviation in news)
      - The race type
      - The 2026 election year
      - Names of known candidates from race_context, if available
    """
    state_names = {
        "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
        "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
        "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
        "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
        "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
        "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
        "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
        "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
        "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
        "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
        "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
        "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
        "WI": "Wisconsin", "WY": "Wyoming",
    }
    state_full = state_names.get((state or "").upper(), state or "")
    parts: list[str] = []
    if state_full:
        parts.append(f'"{state_full}"')
    rt = (race_type or "").lower()
    if rt in ("senate", "house", "governor"):
        parts.append(rt)
    # Pull candidate surnames out of context (max 4) for query specificity.
    if race_context:
        names: list[str] = []
        for c in (race_context.get("candidates") or [])[:4]:
            name = c.get("name") if isinstance(c, dict) else None
            if name:
                surname = name.split()[-1]
                if surname and len(surname) > 2:
                    names.append(f'"{surname}"')
        if names:
            parts.append("(" + " OR ".join(names) + ")")
    parts.append("2026")
    return " AND ".join(parts)


async def fetch_articles_for_race(
    session: aiohttp.ClientSession,
    *,
    race_type: str,
    state: str,
    window_hours: int = 48,
    race_context: dict | None = None,
    end_ts: datetime | None = None,
    max_articles: int = 12,
) -> list[dict]:
    """Fetch news articles plausibly relevant to a race within a time window.

    Tries NewsAPI first (better metadata), then GDELT, then dedupes by URL.
    Articles are clipped to those published in [end_ts - window_hours, end_ts]
    so the LLM can only reason from temporally plausible inputs.
    """
    end_ts = end_ts or datetime.now(timezone.utc)
    from_ts = end_ts - timedelta(hours=max(1, window_hours))
    query = _build_query(race_type, state, race_context)

    articles: list[dict] = []
    try:
        articles.extend(await _fetch_newsapi(session, query, from_ts, end_ts, max_articles))
    except Exception as e:
        logger.warning(f"NewsAPI fetch failed: {e}")
    if len(articles) < max_articles:
        try:
            articles.extend(await _fetch_gdelt(session, query, from_ts, end_ts, max_articles - len(articles)))
        except Exception as e:
            logger.warning(f"GDELT fetch failed: {e}")

    # Dedupe by URL and apply the hard timing filter (no future-dated articles).
    seen: set[str] = set()
    out: list[dict] = []
    for a in articles:
        url = (a.get("url") or "").strip()
        if not url or url in seen:
            continue
        pub = _iso_to_dt(a.get("published_at") or "")
        if pub and pub > end_ts:
            continue
        seen.add(url)
        out.append(a)
        if len(out) >= max_articles:
            break

    return out


def channels_available() -> dict:
    """Which news providers are configured."""
    return {
        "newsapi": bool(os.getenv("NEWS_API_KEY", "").strip()),
        "gdelt": True,  # always available, no key needed
    }
