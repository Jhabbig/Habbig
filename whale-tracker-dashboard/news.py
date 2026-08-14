"""Market-news ingestion: free RSS feeds + optional Benzinga REST adapter.

Two sources land in one table (`news_item`):

  - RSS/Atom feeds (free, no key). Parsed with stdlib xml.etree — we handle
    both RSS 2.0 ``<item>`` and Atom ``<entry>`` shapes by matching on local
    tag names, so namespaced feeds work without registering namespaces.
    Defaults: SEC press releases + GlobeNewswire public-companies wire;
    override with NEWS_FEEDS (comma-separated URLs).
  - Benzinga newsfeed REST API (paid, key required). Only activates when
    BENZINGA_API_KEY is set. The response shape is normalised defensively
    (list vs. wrapper dict, `title` vs. `headline`, `stocks` as dicts or
    strings) because the live shape can't be verified from the sandbox.

Ticker tagging is heuristic: press releases conventionally cite the listing
as "(NYSE: ABC)" / "(Nasdaq: ABC)" / "(OTC: ABCD)" etc., so we regex the
title+summary for exchange patterns and store the unique uppercase symbols
comma-joined in `tickers`. Wire stories that never cite an exchange get an
empty tickers column — absence of a tag is NOT absence of relevance.

Caveats: RSS summaries may contain HTML (we strip tags crudely); publish
dates arrive as RFC-822 or ISO-8601 and are normalised to UTC ISO-8601,
falling back to the raw string when unparseable (such rows may sort oddly).
Every network call degrades to an empty result on failure — a dead feed
must never crash refresh().
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx

import db
import events

log = logging.getLogger("news")

USER_AGENT = "narve.ai whale tracker contact@narve.ai"
_TIMEOUT = 20.0
_sem = asyncio.Semaphore(4)

DEFAULT_FEEDS = [
    "https://www.sec.gov/news/pressreleases.rss",
    "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/"
    "GlobeNewswire%20-%20News%20about%20Public%20Companies",
]

BENZINGA_NEWS_URL = "https://api.benzinga.com/api/v2/news"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_item (
    item_id      TEXT PRIMARY KEY,   -- sha1(url), or sha1(title|published) when url missing
    source       TEXT,               -- feed host or 'benzinga'
    published_at TEXT,               -- UTC ISO-8601 when parseable, else raw string
    title        TEXT,
    summary      TEXT,
    url          TEXT,
    tickers      TEXT                -- comma-joined unique uppercase symbols, '' when none
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_item(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_source ON news_item(source, published_at DESC);
"""

_schema_ready = False


def ensure_schema() -> None:
    """Create our tables/indexes if missing. Idempotent; cached after first run."""
    global _schema_ready
    if _schema_ready:
        return
    with db.connect() as cx:
        cx.executescript(_SCHEMA)
    _schema_ready = True


# ─── Config ───────────────────────────────────────────────────────────

def _feed_urls() -> list[str]:
    raw = os.environ.get("NEWS_FEEDS", "").strip()
    if not raw:
        return list(DEFAULT_FEEDS)
    return [u.strip() for u in raw.split(",") if u.strip()]


def _max_per_feed() -> int:
    try:
        return max(1, int(os.environ.get("NEWS_MAX_PER_FEED", "50")))
    except ValueError:
        return 50


def is_benzinga_configured() -> bool:
    return bool(os.environ.get("BENZINGA_API_KEY", "").strip())


# ─── Ticker tagging ──────────────────────────────────────────────────

# Matches "(NYSE: ABC)", "(Nasdaq ABC)", "(NYSE American: ABC)", "(OTC: ABCD)",
# with colon or space between exchange and symbol. The exchange name is
# case-insensitive; the symbol must be uppercase in the source text (filtered
# in _extract_tickers) so prose like "(Nasdaq: rally)" doesn't tag.
_TICKER_RX = re.compile(
    r"\(\s*"
    r"(?:NYSE(?:\s+American|\s+Arca|\s+MKT)?"
    r"|NASDAQ(?:\s+Global(?:\s+Select)?(?:\s+Market)?)?"
    r"|AMEX|CBOE|OTC(?:QB|QX)?(?:\s+Markets)?|TSXV?)"
    r"\s*[:\s]\s*"
    r"([A-Za-z]{1,5})"
    r"\s*\)",
    re.IGNORECASE,
)


def _extract_tickers(text: str) -> str:
    """Unique uppercase 1-5 char symbols from exchange citations, comma-joined."""
    if not text:
        return ""
    symbols = {m for m in _TICKER_RX.findall(text) if m.isupper()}
    return ",".join(sorted(symbols))


# ─── Date normalisation ──────────────────────────────────────────────

def _normalize_date(raw: str | None) -> str:
    """RFC-822 or ISO-8601 → UTC 'YYYY-MM-DDTHH:MM:SSZ'; raw string on failure."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    parsed: dt.datetime | None = None
    try:
        parsed = parsedate_to_datetime(raw)
    except Exception:
        parsed = None
    if parsed is None:
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── RSS / Atom ──────────────────────────────────────────────────────

def _localname(tag: Any) -> str:
    """'{http://…/Atom}entry' → 'entry'. Non-str tags (comments/PIs) → ''."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


_HTML_TAG_RX = re.compile(r"<[^>]+>")
_WS_RX = re.compile(r"\s+")


def _clean_text(text: str | None, limit: int = 2000) -> str:
    """Strip embedded HTML tags, unescape entities, collapse whitespace."""
    if not text:
        return ""
    cleaned = html.unescape(_HTML_TAG_RX.sub(" ", text))
    return _WS_RX.sub(" ", cleaned).strip()[:limit]


def _item_link(item: ET.Element) -> str:
    """RSS <link>text</link> or Atom <link href=… rel=alternate>."""
    alternate = ""
    first = ""
    for child in item:
        if _localname(child.tag) != "link":
            continue
        href = (child.get("href") or child.text or "").strip()
        if not href:
            continue
        if not first:
            first = href
        rel = (child.get("rel") or "alternate").lower()
        if rel == "alternate" and not alternate:
            alternate = href
    return alternate or first


def _parse_feed(body: str, source: str) -> list[dict]:
    """Parse an RSS 2.0 or Atom body into normalised rows. Never raises."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        log.info("feed %s parse failed: %s", source, e)
        return []

    out: list[dict] = []
    for item in root.iter():
        if _localname(item.tag) not in ("item", "entry"):
            continue
        title = ""
        summary = ""
        published = ""
        for child in item:
            name = _localname(child.tag)
            if name == "title" and not title:
                title = _clean_text(child.text, limit=500)
            elif name in ("description", "summary", "content") and not summary:
                summary = _clean_text(child.text)
            elif name in ("pubdate", "published", "updated", "date") and not published:
                published = (child.text or "").strip()
        url = _item_link(item)
        if not title and not url:
            continue
        published_at = _normalize_date(published)
        out.append({
            "item_id":      _item_id(url, title, published_at),
            "source":       source,
            "published_at": published_at,
            "title":        title,
            "summary":      summary,
            "url":          url,
            "tickers":      _extract_tickers(f"{title} {summary}"),
        })
    return out


def _item_id(url: str, title: str, published_at: str) -> str:
    basis = url.strip() or f"{title}|{published_at}"
    return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()


def _feed_source(url: str) -> str:
    try:
        host = (urlparse(url).netloc or "").lower()
    except ValueError:
        host = ""
    return host.removeprefix("www.") or url[:60]


async def _fetch_feed(url: str) -> list[dict]:
    """Fetch + parse one feed. Returns [] on any failure."""
    async with _sem:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            ) as cx:
                r = await cx.get(url)
                r.raise_for_status()
                body = r.text
        except Exception as e:
            log.info("feed fetch %s failed: %s", url, e)
            return []
    return _parse_feed(body, source=_feed_source(url))[: _max_per_feed()]


# ─── Benzinga ────────────────────────────────────────────────────────

def _benzinga_items(data: Any) -> list[dict]:
    """Unwrap the response body into a list of article dicts, defensively."""
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = None
        for key in ("news", "data", "articles", "results", "items"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if items is None:
            items = []
    else:
        items = []
    return [it for it in items if isinstance(it, dict)]


def _benzinga_tickers(item: dict) -> set[str]:
    """`stocks` may be a list of dicts ({'name': 'AAPL'}) or plain strings."""
    out: set[str] = set()
    stocks = item.get("stocks") or item.get("tickers") or []
    if not isinstance(stocks, list):
        return out
    for s in stocks:
        sym = ""
        if isinstance(s, dict):
            sym = str(s.get("name") or s.get("symbol") or "").strip()
        elif isinstance(s, str):
            sym = s.strip()
        sym = sym.upper()
        if sym and len(sym) <= 8 and re.fullmatch(r"[A-Z0-9.\-]+", sym):
            out.add(sym)
    return out


async def _fetch_benzinga() -> list[dict]:
    """Pull latest Benzinga headlines. Returns [] when unconfigured or failing."""
    key = os.environ.get("BENZINGA_API_KEY", "").strip()
    if not key:
        return []
    async with _sem:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                follow_redirects=True,
            ) as cx:
                r = await cx.get(
                    BENZINGA_NEWS_URL,
                    params={"token": key, "pageSize": _max_per_feed()},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.info("benzinga fetch failed: %s", e)
            return []

    out: list[dict] = []
    for it in _benzinga_items(data):
        title = _clean_text(str(it.get("title") or it.get("headline") or ""), limit=500)
        url = str(it.get("url") or it.get("link") or "").strip()
        if not title and not url:
            continue
        summary = _clean_text(str(it.get("teaser") or it.get("summary") or it.get("body") or ""))
        published_at = _normalize_date(
            str(it.get("created") or it.get("published") or it.get("updated") or "")
        )
        tickers = _benzinga_tickers(it)
        regex_hit = _extract_tickers(f"{title} {summary}")
        if regex_hit:
            tickers |= set(regex_hit.split(","))
        out.append({
            "item_id":      _item_id(url, title, published_at),
            "source":       "benzinga",
            "published_at": published_at,
            "title":        title,
            "summary":      summary,
            "url":          url,
            "tickers":      ",".join(sorted(tickers)),
        })
    return out[: _max_per_feed()]


# ─── Public API ──────────────────────────────────────────────────────

async def refresh() -> int:
    """Pull all configured feeds (+ Benzinga if keyed). Returns NEW rows stored."""
    ensure_schema()
    tasks = [_fetch_feed(u) for u in _feed_urls()]
    if is_benzinga_configured():
        tasks.append(_fetch_benzinga())
    batches = await asyncio.gather(*tasks)

    # Dedupe by item_id across sources before hitting the DB.
    rows: dict[str, dict] = {}
    for batch in batches:
        for row in batch:
            rows.setdefault(row["item_id"], row)

    new = 0
    if rows:
        with db.connect() as cx:
            for row in rows.values():
                cur = cx.execute(
                    """
                    INSERT OR IGNORE INTO news_item (
                        item_id, source, published_at, title, summary, url, tickers
                    ) VALUES (
                        :item_id, :source, :published_at, :title, :summary, :url, :tickers
                    )
                    """,
                    row,
                )
                new += cur.rowcount
    if new:
        events.broadcast("news", {"new_items": new, "feeds": len(batches)})
    log.info("news refresh: %d fetched, %d new", len(rows), new)
    return new


def recent(days: int = 7, limit: int = 100, ticker: str | None = None) -> list[dict]:
    """Latest stored items, optionally filtered to one tagged ticker.

    The tickers column is comma-joined, so the filter wraps both sides in
    commas and LIKEs on ',SYM,' — 'A' can never match inside 'AAPL'.
    """
    ensure_schema()
    sql = ["SELECT * FROM news_item WHERE published_at >= date('now', ?)"]
    params: list[Any] = [f"-{int(days)} days"]
    if ticker:
        sym = re.sub(r"[^A-Z0-9.\-]", "", ticker.upper().strip())
        if not sym:
            return []
        sql.append("AND tickers != '' AND (',' || tickers || ',') LIKE ?")
        params.append(f"%,{sym},%")
    sql.append("ORDER BY published_at DESC LIMIT ?")
    params.append(int(limit))
    with db.connect() as cx:
        found = cx.execute(" ".join(sql), params).fetchall()
    return [dict(r) for r in found]
