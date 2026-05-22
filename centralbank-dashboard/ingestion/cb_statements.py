"""Central bank monetary-policy statement feeds.

Pulls each CB's official press-release RSS, filters to monetary-policy items
by title keyword, and exposes the title + summary + link. The scorer then
runs against the summary text — full statement text would require fetching
and parsing each linked HTML page, which is fragile across CB websites.
The summary is the first paragraph or two of the statement, which carries
most of the stance signal in practice.

Sources (all public RSS, no key):
  - Fed FOMC press releases: https://www.federalreserve.gov/feeds/press_monetary.xml
  - ECB press releases:      https://www.ecb.europa.eu/rss/press.xml
  - BoE news:                https://www.bankofengland.co.uk/rss/news

XML is parsed via `defusedxml` for XXE safety — same convention as
`world-state-dashboard`.

Cache: 1 h. Statements come out at most ~8x/year per CB; tighter just
hammers the source.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock

try:
    from defusedxml.ElementTree import fromstring as xml_fromstring
except ImportError:  # pragma: no cover
    raise ImportError("defusedxml is required: pip install defusedxml")

log = logging.getLogger(__name__)

_UA = "centralbank-dashboard/0.1"
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_CACHE_TTL = 60 * 60  # 1 h
_lock = Lock()


@dataclass
class CBFeed:
    cb: str  # "US" | "EA" | "UK"
    cb_name: str
    rss_url: str
    # Title-filter regex — must match for an item to be considered monetary-policy
    title_filter: re.Pattern


FEEDS: list[CBFeed] = [
    CBFeed(
        cb="US",
        cb_name="Federal Reserve",
        # The monetary-policy feed is already filtered to FOMC-related items,
        # but we still apply a keyword filter as a defensive layer.
        rss_url="https://www.federalreserve.gov/feeds/press_monetary.xml",
        title_filter=re.compile(r"\b(fomc|monetary|federal funds|policy)\b", re.I),
    ),
    CBFeed(
        cb="EA",
        cb_name="European Central Bank",
        rss_url="https://www.ecb.europa.eu/rss/press.xml",
        title_filter=re.compile(
            r"\b(monetary policy|interest rate|key ecb|deposit facility|rate decision)\b",
            re.I,
        ),
    ),
    CBFeed(
        cb="UK",
        cb_name="Bank of England",
        rss_url="https://www.bankofengland.co.uk/rss/news",
        title_filter=re.compile(
            r"\b(monetary policy|bank rate|mpc|interest rate)\b", re.I,
        ),
    ),
]


_HTML_TAG_RX = re.compile(r"<[^>]+>")
_WS_RX = re.compile(r"\s+")
# Strip <script> and <style> blocks entirely — their contents would otherwise
# leak into the visible-text fallback when we drop tags.
_SCRIPT_STYLE_RX = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_HTML_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'",
    "&nbsp;": " ", "&#160;": " ", "&#39;": "'", "&#8217;": "’", "&#8220;": "“",
    "&#8221;": "”", "&#8211;": "–", "&#8212;": "—",
}


def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = _SCRIPT_STYLE_RX.sub(" ", s)
    s = _HTML_TAG_RX.sub(" ", s)
    for k, v in _HTML_ENTITIES.items():
        s = s.replace(k, v)
    return _WS_RX.sub(" ", s).strip()


# Cache fetched statement bodies by URL — these don't change once published.
_BODY_CACHE: dict[str, str] = {}
_BODY_CACHE_LIMIT = 50


def _fetch_statement_body(url: str, max_chars: int = 20000) -> str:
    """Fetch the linked press-release page and return clean visible text.

    Crude but robust: strips all HTML/scripts/styles, normalizes whitespace.
    Returns empty string on any failure — caller falls back to RSS summary.
    """
    if not url or not url.startswith(("http://", "https://")):
        return ""
    if url in _BODY_CACHE:
        return _BODY_CACHE[url]
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            raw = resp.read(2_000_000).decode("utf-8", errors="replace")  # cap at 2 MB
    except Exception as exc:
        log.warning("Statement body fetch failed for %s: %s", url, exc)
        return ""
    text = _strip_html(raw)[:max_chars]
    # Trim cache aggressively — these are big strings.
    if len(_BODY_CACHE) >= _BODY_CACHE_LIMIT:
        _BODY_CACHE.clear()
    _BODY_CACHE[url] = text
    return text


def _fetch(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/rss+xml, application/xml"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted hosts)
        return resp.read()


def _parse_rss(body: bytes, feed: CBFeed, max_items: int = 5) -> list[dict]:
    """Return up to `max_items` matching items, newest first."""
    try:
        root = xml_fromstring(body)
    except Exception as exc:
        log.warning("RSS parse failed for %s: %s", feed.cb, exc)
        return []

    # RSS 2.0: rss > channel > item; Atom: feed > entry. Try both.
    items: list = []
    for it in root.iter():
        tag = it.tag.split("}")[-1].lower()
        if tag in ("item", "entry"):
            items.append(it)

    out: list[dict] = []
    for it in items:
        get = lambda name: next(
            (c.text for c in it if c.tag.split("}")[-1].lower() == name and c.text),
            "",
        )
        title = get("title")
        if not title or not feed.title_filter.search(title):
            continue
        link = get("link")
        if not link:
            # Atom links are often in attribs
            for c in it:
                if c.tag.split("}")[-1].lower() == "link" and c.attrib.get("href"):
                    link = c.attrib["href"]
                    break
        # Description / summary (carries stance signal)
        summary = get("description") or get("summary") or get("content")
        published = get("pubDate") or get("published") or get("updated")
        published_iso = ""
        if published:
            try:
                dt = parsedate_to_datetime(published)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                published_iso = dt.astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                published_iso = published  # leave as-is

        out.append({
            "title": title.strip(),
            "link": link.strip(),
            "summary": _strip_html(summary),
            "published": published_iso,
        })
        if len(out) >= max_items:
            break
    return out


def fetch_latest_for_feed(feed: CBFeed, max_items: int = 5) -> list[dict]:
    try:
        body = _fetch(feed.rss_url)
    except Exception as exc:
        log.warning("RSS fetch failed for %s (%s): %s", feed.cb, feed.rss_url, exc)
        return []
    return _parse_rss(body, feed, max_items=max_items)


def fetch_all() -> list[dict]:
    """Returns the latest matching item per CB (just one — the most recent
    monetary-policy press release). For each item we attempt to fetch the
    full HTML body so the stance scorer has real text to work with; falls
    back to the RSS summary if the linked page isn't reachable."""
    out: list[dict] = []
    for feed in FEEDS:
        items = fetch_latest_for_feed(feed, max_items=1)
        latest = items[0] if items else None
        if latest:
            body = _fetch_statement_body(latest.get("link", ""))
            # Use full body when we got one; fall back to summary; never empty.
            latest["body_text"] = body
            latest["scoring_text"] = body if len(body) > 200 else latest.get("summary", "")
            latest["scoring_source"] = "body" if len(body) > 200 else "summary"
        out.append({
            "cb": feed.cb,
            "cb_name": feed.cb_name,
            "rss_url": feed.rss_url,
            "latest": latest,  # may be None
        })
    return out


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = _CACHE["data"] is not None and (now - _CACHE["fetched_at"]) < _CACHE_TTL
        if fresh and not force:
            return _CACHE["data"]
    data = {
        "fetched_at": now,
        "feeds": fetch_all(),
    }
    with _lock:
        _CACHE["data"] = data
        _CACHE["fetched_at"] = now
    return data


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(get_cached(force=True), indent=2)[:2000])
