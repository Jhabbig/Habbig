"""AI news RSS fan-in.

Mirrors the proven world-state-dashboard pattern (defusedxml-parsed RSS/Atom,
≤90s cache, per-feed best-effort with graceful degradation). Sources live in
`data.NEWS_FEEDS`; entries are merged + sorted by pub date.

Output shape (per item):
    {
      "source":      "Anthropic",
      "kind":        "lab" | "research" | "newsletter" | "community",
      "title":       str,
      "link":        str,
      "pub_date":    raw RFC822/ISO str,
      "ts":          float (unix seconds, for sorting),
      "description": str (HTML-stripped, ≤220 chars),
    }
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ImportError as _exc:  # pragma: no cover
    raise ImportError("defusedxml is required: pip install defusedxml") from _exc

import data as ai_data

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (AIRaceDashboard/1.0; +news)"
_TTL = 90
_lock = threading.Lock()
_cache: dict = {"data": [], "fetched_at": 0.0}


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:  # noqa: BLE001
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _is_safe_link(link: str) -> bool:
    return link.startswith(("http://", "https://"))


def _fetch_feed(feed: dict, per_feed_cap: int = 12) -> list[dict]:
    try:
        req = urllib.request.Request(feed["url"], headers={"User-Agent": _UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            xml_data = resp.read()
        root = _xml_fromstring(xml_data)
    except Exception as e:  # noqa: BLE001
        log.warning("news fetch failed (%s): %s", feed["name"], e)
        return []

    items = root.findall(".//item")
    is_atom = not items
    if is_atom:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        items = root.findall("a:entry", ns)

    out: list[dict] = []
    for item in items[:per_feed_cap]:
        try:
            if is_atom:
                ns = {"a": "http://www.w3.org/2005/Atom"}
                title = (item.findtext("a:title", default="", namespaces=ns) or "").strip()
                # Atom link is an element with href attr
                link_el = item.find("a:link", ns)
                link = (link_el.get("href") if link_el is not None else "") or ""
                pub_date = (item.findtext("a:updated", default="", namespaces=ns)
                            or item.findtext("a:published", default="", namespaces=ns)
                            or "").strip()
                desc = _strip_html(item.findtext("a:summary", default="", namespaces=ns) or "")
            else:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date = (item.findtext("pubDate")
                            or item.findtext("{http://purl.org/dc/elements/1.1/}date")
                            or "").strip()
                desc = _strip_html(item.findtext("description") or "")
        except Exception as e:  # noqa: BLE001
            log.warning("news item parse error (%s): %s", feed["name"], e)
            continue

        if not title:
            continue
        if not _is_safe_link(link):
            link = ""
        out.append({
            "source": feed["name"],
            "kind": feed.get("kind", "other"),
            "title": title[:240],
            "link": link,
            "pub_date": pub_date,
            "ts": _parse_date(pub_date),
            "description": desc[:220],
        })
    return out


def get_news(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        if not force and _cache["data"] and (now - _cache["fetched_at"]) < _TTL:
            return {"items": _cache["data"], "fetched_at": _cache["fetched_at"]}

    items: list[dict] = []
    per_feed_status: list[dict] = []
    for feed in ai_data.NEWS_FEEDS:
        chunk = _fetch_feed(feed)
        per_feed_status.append({
            "name": feed["name"],
            "kind": feed.get("kind", "other"),
            "count": len(chunk),
            "ok": len(chunk) > 0,
        })
        items.extend(chunk)

    items.sort(key=lambda i: i["ts"], reverse=True)
    items = items[:80]
    with _lock:
        _cache["data"] = items
        _cache["fetched_at"] = now
    return {"items": items, "fetched_at": now, "sources": per_feed_status}
