"""Pinterest trends — top searches.

Pinterest publishes trending searches at trends.pinterest.com. The page
embeds the trends JSON in a <script> tag; we grab that.
"""

from __future__ import annotations

import json
import logging
import os
import re

from models import Item
from ._http import client

NAME = "pinterest_trends"
SECTION = "lifestyle"
REFRESH_SECONDS = 12 * 60 * 60

log = logging.getLogger(__name__)


async def fetch() -> list[Item]:
    geo = os.environ.get("CULTURE_GEO", "US")
    url = f"https://trends.pinterest.com/?country={geo}&latest_trends_per_page=25"
    async with client() as c:
        r = await c.get(url)
        if r.status_code != 200:
            return []
        html = r.text
    blob = _extract_initial_state(html)
    if not blob:
        return []
    trends = _walk_for_trends(blob)
    items: list[Item] = []
    for i, t in enumerate(trends[:25]):
        keyword = t.get("keyword") or t.get("query") or ""
        if not keyword:
            continue
        weekly = float(t.get("weeklyChange") or t.get("weekly_change") or 0)
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=keyword,
            url=f"https://www.pinterest.com/search/pins/?q={keyword.replace(' ', '%20')}",
            image=t.get("image") or t.get("imageUrl"),
            score=float(25 - i) + abs(weekly),
            velocity=weekly,
            extra={"rank": i + 1, "category": t.get("category")},
        ))
    return items


def _extract_initial_state(html: str) -> dict | None:
    m = re.search(
        r'<script[^>]*id="__PWS_INITIAL_PROPS__"[^>]*>(.+?)</script>', html, re.DOTALL,
    )
    if not m:
        m = re.search(r'<script[^>]*type="application/json"[^>]*>(.+?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _walk_for_trends(blob: dict | list, _depth: int = 0) -> list[dict]:
    """Find the first list of dicts that looks like trend records."""
    if _depth > 8:
        return []
    if isinstance(blob, dict):
        for k, v in blob.items():
            if k.lower() in ("trends", "topkeywords", "top_keywords", "results") \
                    and isinstance(v, list) and v and isinstance(v[0], dict) \
                    and ("keyword" in v[0] or "query" in v[0]):
                return v
            found = _walk_for_trends(v, _depth + 1)
            if found:
                return found
    elif isinstance(blob, list):
        for v in blob:
            found = _walk_for_trends(v, _depth + 1)
            if found:
                return found
    return []
