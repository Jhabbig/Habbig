"""
Per-country political news feed.

Source: Google News RSS — no API key, no rate limits we hit at this scale,
returns clean XML with title/link/source/pubDate/description. Each country
gets a query like "<country name> politics OR election OR government" and
the top 12 hits become the feed.

Caching is aggressive (10 min) since this is a directional indicator —
people open the drawer to see "what's the recent news here", not for
breaking-news minute-precision.

Public surface:
    await fetch_news_for_country(iso, name) -> {items: [...], _status: {...}}

If Google News is unreachable we return an empty items list with
_status.ok=False so the UI can show "news unavailable" rather than
fail the whole drawer.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

# Use defusedxml — Google News RSS is untrusted input. Stdlib's
# ElementTree is vulnerable to billion-laughs / XXE / entity-expansion
# attacks; defusedxml is a drop-in replacement that disables those.
import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger("voters.news")

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
REQUEST_TIMEOUT = 6.0
CACHE_TTL = 600  # 10 min — political news changes on hour-scale
NEGATIVE_CACHE_TTL = 120  # 2 min — retry sooner if upstream failed
MAX_ITEMS = 12

# in-memory cache: ISO3 -> {items: [...], fetched_at: float, ok: bool}
_cache: dict[str, dict[str, Any]] = {}
_locks: dict[str, asyncio.Lock] = {}


# Per-country query overrides — when the country name alone is too generic
# (e.g. "Georgia" matches both the country and the US state), force a
# disambiguating phrase.
_QUERY_OVERRIDES: dict[str, str] = {
    "USA": "United States politics OR election OR Congress",
    "GBR": "United Kingdom politics OR election OR Parliament Westminster",
    "DEU": "Germany politics OR election OR Bundestag",
    "FRA": "France politics OR election OR government",
    "ISR": "Israel politics OR Netanyahu OR Knesset",
    "KOR": "South Korea politics OR election OR National Assembly",
    "TWN": "Taiwan politics OR election OR Lai",
    "IRN": "Iran politics OR election OR Pezeshkian OR Khamenei",
    "ARG": "Argentina politics OR Milei OR election",
    "TUR": "Turkey politics OR Erdogan OR election",
    "ZAF": "South Africa politics OR election OR ANC",
    "VEN": "Venezuela politics OR Maduro OR election",
    "EGY": "Egypt politics OR Sisi OR parliament",
    "UKR": "Ukraine politics OR Zelensky OR Verkhovna Rada",
    "PAK": "Pakistan politics OR Sharif OR Imran Khan",
    "NGA": "Nigeria politics OR Tinubu OR election",
    "MEX": "Mexico politics OR Sheinbaum OR Morena",
    "THA": "Thailand politics OR Pheu Thai OR Paetongtarn",
}


def _query_for_country(iso: str, name: str) -> str:
    """Build the query string we'll send to Google News RSS."""
    iso = (iso or "").upper()
    if iso in _QUERY_OVERRIDES:
        return _QUERY_OVERRIDES[iso]
    label = name or iso
    return f"{label} politics OR election OR government"


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&[a-z]+;", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _parse_rss(xml_text: str) -> list[dict]:
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, DefusedXmlException) as e:
        # ParseError = malformed XML; DefusedXmlException = defusedxml
        # blocked something dangerous (entity expansion, external DTD, etc.)
        # Either way, drop the feed silently — better than 500-ing the request.
        log.warning("news rss parse failed: %s", e)
        return []
    # RSS 2.0: channel/item under <rss>
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        # source can be: <source url="https://...">Reuters</source>
        source_el = item.find("source")
        source = (source_el.text if source_el is not None else "").strip() or None
        description = (item.findtext("description") or "").strip()
        summary = _strip_html(description)[:280] or None
        if not (title and link):
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "source": source,
                "published": pub,
                "summary": summary,
            }
        )
        if len(items) >= MAX_ITEMS:
            break
    return items


async def _fetch(query: str) -> list[dict]:
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    url = f"{GOOGLE_NEWS_RSS}?{urlencode(params)}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 voters-atlas"})
            r.raise_for_status()
            return _parse_rss(r.text)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("news fetch failed for %r: %s", query, e)
        return []


async def fetch_news_for_country(iso: str, name: str = "") -> dict[str, Any]:
    """Public entrypoint. Returns {items, _status} with TTL-cached results."""
    iso = (iso or "").upper()
    if not iso:
        return {"items": [], "_status": {"ok": False, "fetched_at": 0}}

    now = time.time()
    cached = _cache.get(iso)
    if cached and (now - cached["fetched_at"]) < CACHE_TTL:
        return {
            "items": cached["items"],
            "_status": {"ok": cached["ok"], "fetched_at": int(cached["fetched_at"])},
        }

    lock = _locks.setdefault(iso, asyncio.Lock())
    async with lock:
        # Re-check after acquiring lock
        cached = _cache.get(iso)
        if cached and (time.time() - cached["fetched_at"]) < CACHE_TTL:
            return {
                "items": cached["items"],
                "_status": {"ok": cached["ok"], "fetched_at": int(cached["fetched_at"])},
            }
        items = await _fetch(_query_for_country(iso, name))
        ok = bool(items)
        # On failure, set fetched_at so we retry after NEGATIVE_CACHE_TTL
        # rather than the full CACHE_TTL.
        fetched_at = time.time() if ok else (time.time() - (CACHE_TTL - NEGATIVE_CACHE_TTL))
        _cache[iso] = {"items": items, "fetched_at": fetched_at, "ok": ok}
        return {
            "items": items,
            "_status": {"ok": ok, "fetched_at": int(fetched_at)},
        }


async def warmup_top(isos: list[str], names: dict[str, str]) -> None:
    """Best-effort prefetch for a list of priority countries on startup."""
    if not isos:
        return
    tasks = [fetch_news_for_country(iso, names.get(iso, "")) for iso in isos]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        log.warning("news warmup failed: %s", e)
