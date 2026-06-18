"""narve multi-source INFO ingest layer.

Pulls RAW INFO (not predictions) from the sources that are actually reachable
from this host and normalises every record into the shape the forecasting
engine (`intelligence.info_forecast`) consumes:

    {"platform": str, "text": str, "author": str|None, "url": str|None,
     "ts": str|None}   # ts is ISO-8601 or epoch when the source provides one

PRODUCT MODEL: narve ingests information, reasons over it, and produces its OWN
probability. These fetchers are the front of that pipe. They never touch a
market price.

REACHABILITY (verified live from this host):
  * Reddit RSS (Atom)      -> platform="reddit"
  * Hacker News (Algolia)  -> platform="hackernews"
  * NPR Politics RSS        -> platform="news"
  * The Hill RSS            -> platform="news"
Bluesky searchPosts (403) and GDELT (IP-throttled) are intentionally absent.

PLATFORM NOTE: `info_forecast._normalise_platform` only recognises
("x", "truthsocial", "reddit", "news"); anything else collapses to "news" for
source_spread purposes. We still tag Hacker News items platform="hackernews"
so the raw stream stays honest about provenance and a future engine could
weight it distinctly. Downstream, that platform currently folds into "news" in
source_spread — this is expected and acceptable.

NETWORK: urllib is 403-blocked from this host, so every call shells out to
`curl` via subprocess with a Mozilla UA. This IP is flaky — an empty body is
NOT "no data", so `_curl` retries on empty (>=3 attempts, 2s sleep).
"""
from __future__ import annotations

import html
import re
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Optional

_UA = "Mozilla/5.0 (narve)"
_ATOM = "{http://www.w3.org/2005/Atom}"

# How aggressively to clean source HTML out of body text.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _log(msg: str) -> None:
    print(f"[info_ingest] {msg}", file=sys.stderr)


def _curl(url: str, *, retries: int = 3, timeout: int = 30) -> str:
    """GET `url` via curl, returning the response body (possibly empty after all
    retries). RETRY-ON-EMPTY: this host's IP intermittently returns an empty
    body on success, so an empty body is retried up to `retries` times (2s
    sleep) before giving up. Never raises on network failure — returns "" so
    callers degrade to zero items rather than crashing the whole gather."""
    for attempt in range(1, retries + 1):
        try:
            proc = subprocess.run(
                ["curl", "-s", "-L", "--max-time", str(timeout), "-A", _UA, url],
                capture_output=True,
                text=True,
            )
        except Exception as exc:  # curl missing / OS error
            _log(f"curl raised {exc!r} (attempt {attempt}/{retries})")
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
        if proc is not None and proc.returncode != 0:
            _log(f"curl exit {proc.returncode} (attempt {attempt}/{retries})")
        else:
            _log(f"empty body (attempt {attempt}/{retries}) — retrying (NOT 'no data')")
        if attempt < retries:
            time.sleep(2)
    _log(f"no body after {retries} attempts for {url}")
    return ""


def _strip_html(s: Optional[str]) -> str:
    """Remove HTML tags, unescape entities, and collapse whitespace."""
    if not s:
        return ""
    txt = _TAG_RE.sub(" ", s)
    txt = html.unescape(txt)
    return _WS_RE.sub(" ", txt).strip()


def _clean(s: Optional[str]) -> str:
    """Collapse whitespace on already-tagless text (titles)."""
    if not s:
        return ""
    return _WS_RE.sub(" ", html.unescape(s)).strip()


# --------------------------------------------------------------------------- #
# Reddit (Atom RSS)
# --------------------------------------------------------------------------- #
def parse_reddit(body: str, limit: int = 25) -> list[dict]:
    """Parse a Reddit Atom feed body into normalised info items. Pure function
    (no network) so it is unit-testable against a saved fixture."""
    if not body or not body.strip():
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        _log(f"reddit parse error: {exc}")
        return []
    items: list[dict] = []
    for entry in root.findall(f"{_ATOM}entry"):
        title = _clean(entry.findtext(f"{_ATOM}title"))
        content = _strip_html(entry.findtext(f"{_ATOM}content"))
        text = (title + " " + content).strip()
        if not text:
            continue
        author_el = entry.find(f"{_ATOM}author")
        author = (
            _clean(author_el.findtext(f"{_ATOM}name")) if author_el is not None else None
        ) or None
        link_el = entry.find(f"{_ATOM}link")
        url = link_el.get("href") if link_el is not None else None
        ts = (entry.findtext(f"{_ATOM}updated") or "").strip() or None
        items.append(
            {"platform": "reddit", "text": text, "author": author, "url": url, "ts": ts}
        )
        if len(items) >= limit:
            break
    return items


def fetch_reddit(subreddit: str, query: Optional[str] = None, limit: int = 25) -> list[dict]:
    """Fetch info items from a subreddit. Uses search.rss when `query` is given,
    otherwise the subreddit's plain .rss feed."""
    if query:
        q = urllib.parse.quote(query)
        url = (
            f"https://www.reddit.com/r/{subreddit}/search.rss"
            f"?q={q}&restrict_sr=1&sort=new&limit={limit}"
        )
    else:
        url = f"https://www.reddit.com/r/{subreddit}/.rss?limit={limit}"
    return parse_reddit(_curl(url), limit=limit)


# --------------------------------------------------------------------------- #
# Hacker News (Algolia JSON)
# --------------------------------------------------------------------------- #
def parse_hackernews(body: str, limit: int = 25) -> list[dict]:
    """Parse an HN Algolia search_by_date JSON body into normalised items.
    Pure function (no network) for deterministic unit tests."""
    if not body or not body.strip():
        return []
    import json

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        _log(f"hackernews parse error: {exc}")
        return []
    items: list[dict] = []
    for hit in (data.get("hits") or []):
        title = _clean(hit.get("title"))
        story = _strip_html(hit.get("story_text"))
        text = (title + " " + story).strip()
        if not text:
            continue
        items.append(
            {
                "platform": "hackernews",
                "text": text,
                "author": hit.get("author") or None,
                "url": hit.get("url") or None,
                "ts": hit.get("created_at") or None,
            }
        )
        if len(items) >= limit:
            break
    return items


def fetch_hackernews(query: str, limit: int = 25) -> list[dict]:
    """Fetch HN stories matching `query`, newest first."""
    q = urllib.parse.quote(query)
    url = (
        f"https://hn.algolia.com/api/v1/search_by_date"
        f"?query={q}&tags=story&hitsPerPage={limit}"
    )
    return parse_hackernews(_curl(url), limit=limit)


# --------------------------------------------------------------------------- #
# RSS 2.0 news feeds (NPR, The Hill)
# --------------------------------------------------------------------------- #
def parse_rss(body: str, limit: int = 25) -> list[dict]:
    """Parse an RSS 2.0 feed body (NPR / The Hill shape) into normalised news
    items. Pure function (no network) for deterministic unit tests."""
    if not body or not body.strip():
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        _log(f"rss parse error: {exc}")
        return []
    items: list[dict] = []
    # <item> can live anywhere under the tree; iter() avoids namespace surprises.
    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        desc = _strip_html(item.findtext("description"))
        text = (title + " " + desc).strip()
        if not text:
            continue
        # dc:creator is namespaced; fall back to None when absent.
        author = None
        for child in item:
            tag = child.tag.split("}")[-1]
            if tag == "creator" and child.text:
                author = _clean(child.text) or None
                break
        items.append(
            {
                "platform": "news",
                "text": text,
                "author": author,
                "url": (item.findtext("link") or "").strip() or None,
                "ts": (item.findtext("pubDate") or "").strip() or None,
            }
        )
        if len(items) >= limit:
            break
    return items


def fetch_npr(limit: int = 25) -> list[dict]:
    """Fetch NPR Politics headlines (RSS 2.0)."""
    return parse_rss(_curl("https://feeds.npr.org/1014/rss.xml"), limit=limit)


def fetch_hill(limit: int = 25) -> list[dict]:
    """Fetch The Hill homenews headlines (RSS 2.0)."""
    return parse_rss(_curl("https://thehill.com/homenews/feed/"), limit=limit)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def gather(
    query: str,
    subreddits: tuple[str, ...] = ("politics",),
    limit_per: int = 15,
) -> list[dict]:
    """Pull `query` across every reachable source, combine, and dedupe.

    Reddit and HN are queried with `query`; NPR and The Hill are whole-feed
    (they have no search endpoint), so their items are topical to politics, not
    the specific query. Dedupe key is (platform, text[:120]). Any single source
    failing (empty body after retries, parse error) yields zero items from that
    source rather than aborting the gather — narve degrades, never crashes.
    """
    combined: list[dict] = []
    for sub in subreddits:
        try:
            combined.extend(fetch_reddit(sub, query=query, limit=limit_per))
        except Exception as exc:  # defensive: one bad source must not kill gather
            _log(f"fetch_reddit({sub!r}) failed: {exc!r}")
    for fetch in (
        lambda: fetch_hackernews(query, limit=limit_per),
        lambda: fetch_npr(limit=limit_per),
        lambda: fetch_hill(limit=limit_per),
    ):
        try:
            combined.extend(fetch())
        except Exception as exc:
            _log(f"source fetch failed: {exc!r}")

    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for it in combined:
        # text is already HTML-stripped by the per-source parsers; key is stable.
        key = (it.get("platform", ""), (it.get("text", "") or "")[:120])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    return deduped


if __name__ == "__main__":  # pragma: no cover - live smoke
    q = sys.argv[1] if len(sys.argv) > 1 else "election"
    print(f"=== live smoke: query={q!r} ===", file=sys.stderr)
    r = fetch_reddit("politics", query=q, limit=10)
    h = fetch_hackernews(q, limit=10)
    n = fetch_npr(limit=10)
    hill = fetch_hill(limit=10)
    print(f"reddit={len(r)} hackernews={len(h)} npr={len(n)} hill={len(hill)}")
    g = gather(q, limit_per=10)
    print(f"gather (deduped) = {len(g)}")
    if g:
        print("sample:", g[0])
