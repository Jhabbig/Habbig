"""Headline ingestion for board events (Google News RSS, keyless).

For a bounded selection of upcoming events, fetches recent headlines from
Google News RSS search (https://news.google.com/rss/search?q=...) and stores
them per market in the news table. Headlines surface in /api/event and
/api/ask responses and ground the LLM forecaster's prompt so its estimate
starts from current reporting rather than memory alone.

XML parsing must be entity-safe (no DTD/entity resolution — the repo has
been bitten by XXE via news feeds before; see voters-dashboard/news.py).

Contract: async refresh(conn, markets: list[dict]) -> int (headlines stored
this pass). Never raises; per-event failures are logged and skipped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

import db

log = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search"
REQUEST_TIMEOUT = 15.0
PAUSE_SECONDS = 0.3
MAX_ITEMS_PER_EVENT = 5
DEFAULT_MAX_EVENTS = 40
SELECTION_HORIZON_DAYS = 7
SKIP_FRESHER_THAN_HOURS = 12

# Mirrors server._MICRO_RE (recurring intraday micro markets — headline
# queries for them are noise). Copied, not imported: ingestion helpers must
# never import the app spine.
_MICRO_RE = re.compile(
    r"\b(up or down|above .* at \d{1,2}:\d{2}\s*(AM|PM)|\d{1,2}:\d{2}\s*(AM|PM)\s*-\s*\d{1,2}:\d{2}\s*(AM|PM)"
    r"|map \d|total rounds|rounds handicap|games total|game handicap|\bo/u \d|over/under \d+\.5|1st half|first half|quarter \d)\b",
    re.IGNORECASE,
)

_TAG_RE = re.compile(r"<[^>]*>")


def _max_events() -> int:
    try:
        return int(os.environ.get("FORECAST_NEWS_MAX_EVENTS", DEFAULT_MAX_EVENTS))
    except ValueError:
        return DEFAULT_MAX_EVENTS


def _uid(m: dict) -> str:
    return m.get("uid") or f"{m.get('venue')}:{m.get('venue_id')}"


def _parse_end(end_date: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((end_date or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _query_for(m: dict) -> str | None:
    """Search query for a market, or None when it should not be fetched.

    Options strike-ladder questions ("SPY closes at or above $630 ...") are
    useless as headline queries, so venue='options' is excluded — except
    direction rows (venue_id kind 'D'), which query the bare symbol instead.
    """
    if (m.get("venue") or "").lower() == "options":
        parts = (m.get("venue_id") or "").split("-")
        if len(parts) >= 3 and parts[-1].startswith("D"):
            symbol = "-".join(parts[:-2])
            if symbol:
                return f"{symbol} stock"
        return None
    terms = db.search_terms(m.get("question") or "")
    return " ".join(terms) if terms else None


def _fresh_uids(conn: sqlite3.Connection) -> set[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=SKIP_FRESHER_THAN_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute("SELECT DISTINCT market_uid FROM news WHERE ts >= ?", (cutoff,)).fetchall()
    return {r["market_uid"] for r in rows}


def _select(conn: sqlite3.Connection, markets: list[dict]) -> list[tuple[str, str]]:
    """(uid, query) picks for this pass: resolving within 7 days, not micro,
    query-able (see _query_for), no headline stored in the last 12h, top
    _max_events() by liquidity."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=SELECTION_HORIZON_DAYS)
    fresh = _fresh_uids(conn)
    picks = []
    for m in markets:
        end = _parse_end(m.get("end_date"))
        if end is None or end < now or end > cutoff:
            continue
        if _MICRO_RE.search(m.get("question") or ""):
            continue
        if (m.get("category") or "").lower() == "sports":
            continue
        query = _query_for(m)
        if not query:
            continue
        uid = _uid(m)
        if uid in fresh:
            continue
        picks.append((uid, query, m.get("liquidity") or 0.0))
    picks.sort(key=lambda t: t[2], reverse=True)
    return [(uid, query) for uid, query, _liq in picks[: _max_events()]]


async def _fetch(client: httpx.AsyncClient, query: str) -> str | None:
    url = RSS_URL + "?" + urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    last: Exception | None = None
    for _attempt in range(2):
        try:
            resp = await client.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last = e
    log.warning("news: fetch failed for %r: %s", query, last)
    return None


def _parse_items(xml_text: str) -> list[dict]:
    """Up to MAX_ITEMS_PER_EVENT items as db.insert_news-shaped dicts (minus
    market_uid). Raises ValueError / ET.ParseError on bad payloads.

    Modern ET.fromstring does not resolve external entities and raises on
    entity declarations, but DTD markup is rejected up front anyway —
    belt-and-braces against XXE / billion-laughs.
    """
    low = (xml_text or "").lower()
    if "<!doctype" in low or "<!entity" in low:
        raise ValueError("DTD/entity markup rejected")
    root = ET.fromstring(xml_text)
    items: list[dict] = []
    for item in root.iter("item"):
        if len(items) >= MAX_ITEMS_PER_EVENT:
            break
        title = _TAG_RE.sub("", item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        if not title or not url:
            continue
        items.append(
            {
                "title": title,
                "source": (item.findtext("source") or "").strip() or None,
                "url": url,
                "published": (item.findtext("pubDate") or "").strip() or None,
            }
        )
    return items


def _count_for(conn: sqlite3.Connection, uid: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM news WHERE market_uid = ?", (uid,)).fetchone()[0]


async def refresh(conn: sqlite3.Connection, markets: list[dict]) -> int:
    try:
        selected = _select(conn, markets)
        if not selected:
            return 0
        total = 0
        async with httpx.AsyncClient(headers={"User-Agent": "narve-forecast/1.0"}, follow_redirects=True) as client:
            for i, (uid, query) in enumerate(selected):
                if i:
                    await asyncio.sleep(PAUSE_SECONDS)
                text = await _fetch(client, query)
                if text is None:
                    continue
                try:
                    items = _parse_items(text)
                except (ValueError, ET.ParseError) as e:
                    log.warning("news: %s feed rejected: %s", uid, e)
                    continue
                # before/after instead of rowcount: UNIQUE(market_uid,url)
                # makes INSERT OR IGNORE silently drop duplicates.
                before = _count_for(conn, uid)
                for it in items:
                    db.insert_news(conn, {"market_uid": uid, **it})
                total += _count_for(conn, uid) - before
        return total
    except Exception as e:
        log.error("news: refresh pass failed: %s", e)
        return 0
