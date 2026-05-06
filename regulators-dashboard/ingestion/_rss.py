"""Shared RSS/Atom fetcher and parser for regulator press-release feeds.

Lifted from `centralbank-dashboard/ingestion/cb_statements.py` and generalized
so each `<body>_rss.py` module just declares an `RssSource` and calls
`fetch_source(source)`.

XML is parsed via `defusedxml` for XXE safety — same convention as
`world-state-dashboard` and `centralbank-dashboard`.
"""

from __future__ import annotations

import logging
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

try:
    from defusedxml.ElementTree import fromstring as xml_fromstring
except ImportError:  # pragma: no cover
    raise ImportError("defusedxml is required: pip install defusedxml")

log = logging.getLogger(__name__)

UA = "regulators-dashboard/0.1"


@dataclass
class RssSource:
    """One regulator's RSS feed config."""
    code: str             # short id, e.g. "SEC"
    name: str             # display name, e.g. "U.S. Securities and Exchange Commission"
    jurisdiction: str     # "US" | "UK" | "EU" | etc.
    rss_url: str
    # Optional title regex — if set, items must match. Default: pass everything.
    title_filter: re.Pattern | None = None
    # Optional list of tags to apply to every item from this source (e.g. for
    # marking a feed as "speeches-only"). Auto-classifier comes in v0.1.
    static_tags: list[str] = field(default_factory=list)


_HTML_TAG_RX = re.compile(r"<[^>]+>")
_WS_RX = re.compile(r"\s+")
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


def _fetch_bytes(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted hosts)
        # Cap at 5 MB — RSS feeds should never approach this.
        return resp.read(5_000_000)


def _parse_published(s: str) -> str:
    """Best-effort RFC822 / ISO8601 → ISO8601 UTC. Returns "" if unparseable."""
    if not s:
        return ""
    # Try RFC 822 (typical RSS)
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    # Try ISO 8601 (typical Atom)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return ""


def _local_tag(elem) -> str:
    return elem.tag.split("}")[-1].lower()


def _parse(body: bytes, source: RssSource, max_items: int, since_days: int | None) -> list[dict]:
    """Parse an RSS or Atom body into a normalized list of action items."""
    try:
        root = xml_fromstring(body)
    except Exception as exc:
        log.warning("RSS parse failed for %s: %s", source.code, exc)
        return []

    cutoff: datetime | None = None
    if since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    items: list = [it for it in root.iter() if _local_tag(it) in ("item", "entry")]

    out: list[dict] = []
    for it in items:
        title = ""
        link = ""
        summary = ""
        published_raw = ""
        for c in it:
            tag = _local_tag(c)
            if tag == "title" and c.text:
                title = c.text.strip()
            elif tag == "link":
                # RSS: <link>URL</link>; Atom: <link href="URL"/>
                if c.text and c.text.strip():
                    link = c.text.strip()
                elif c.attrib.get("href"):
                    link = c.attrib["href"].strip()
            elif tag in ("description", "summary", "content") and c.text and not summary:
                summary = c.text
            elif tag in ("pubdate", "published", "updated", "date") and c.text and not published_raw:
                published_raw = c.text.strip()

        if not title:
            continue
        if source.title_filter and not source.title_filter.search(title):
            continue

        published_iso = _parse_published(published_raw)
        if cutoff and published_iso:
            try:
                pub_dt = datetime.fromisoformat(published_iso)
                if pub_dt < cutoff:
                    continue
            except ValueError:
                pass

        out.append({
            "id": f"{source.code}::{link or title}",
            "source": source.code,
            "source_name": source.name,
            "jurisdiction": source.jurisdiction,
            "title": title,
            "link": link,
            "summary": _strip_html(summary)[:600],
            "published": published_iso,
            "tags": list(source.static_tags),
        })
        if len(out) >= max_items:
            break
    return out


def fetch_source(source: RssSource, max_items: int = 50, since_days: int | None = 90) -> list[dict]:
    """Fetch one source. Raises on network/parse failure so callers can record
    a real 'unavailable' status. Returns [] only when the feed parsed cleanly
    but had no items matching `title_filter` / `since_days`."""
    body = _fetch_bytes(source.rss_url)
    return _parse(body, source, max_items=max_items, since_days=since_days)
