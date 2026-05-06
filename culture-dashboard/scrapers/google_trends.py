"""Google daily trending searches via the public RSS feed.

Avoids `pytrends` (requires sync HTTP and breaks regularly). The RSS feed
is the same data Google itself surfaces on trends.google.com.
"""

from __future__ import annotations

import logging
import os

import feedparser
from defusedxml.ElementTree import fromstring as xml_fromstring

from models import Item
from ._http import client

NAME = "google_trends"
SECTION = "attention"
REFRESH_SECONDS = 60 * 60

log = logging.getLogger(__name__)

GEO = os.environ.get("CULTURE_GEO", "US")


async def fetch() -> list[Item]:
    url = f"https://trends.google.com/trending/rss?geo={GEO}"
    async with client() as c:
        r = await c.get(url)
        r.raise_for_status()
        body = r.text
    items: list[Item] = []
    parsed = feedparser.parse(body)
    if not parsed.entries:
        items.extend(_fallback_xml(body))
        return items
    for e in parsed.entries[:30]:
        traffic = _extract_traffic(e)
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=e.get("title") or "(untitled)",
            url=e.get("link"),
            summary=(e.get("summary") or "")[:300],
            score=float(traffic),
            extra={"published": e.get("published")},
        ))
    return items


def _extract_traffic(e: dict) -> int:
    """Google adds an `ht:approx_traffic` tag like '500,000+'."""
    raw = e.get("ht_approx_traffic") or "0"
    digits = "".join(ch for ch in raw if ch.isdigit())
    try:
        return int(digits)
    except ValueError:
        return 0


def _fallback_xml(body: str) -> list[Item]:
    """Some feedparser versions drop the ht: namespace — manual parse."""
    items: list[Item] = []
    try:
        root = xml_fromstring(body.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return items
    ns = {"ht": "https://trends.google.com/trending/rss"}
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        traffic_raw = it.findtext("ht:approx_traffic", default="0", namespaces=ns) or "0"
        digits = "".join(ch for ch in traffic_raw if ch.isdigit())
        score = float(int(digits)) if digits else 0.0
        items.append(Item(section=SECTION, source=NAME, title=title or "(untitled)",
                          url=link or None, score=score))
    return items[:30]
