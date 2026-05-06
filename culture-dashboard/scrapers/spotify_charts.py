"""Spotify Top 50 Global — via the public chart page.

Spotify's official charts API is gated for partners only. The public
charts page exposes the same data via embedded JSON. Best-effort: if the
page format changes the scraper returns [] (handled by scheduler).
"""

from __future__ import annotations

import json
import logging
import re

from models import Item
from ._http import client

NAME = "spotify_top50"
SECTION = "entertainment"
REFRESH_SECONDS = 12 * 60 * 60

log = logging.getLogger(__name__)

URL = "https://charts.spotify.com/charts/view/regional-global-daily/latest"


async def fetch() -> list[Item]:
    async with client() as c:
        r = await c.get(URL)
        if r.status_code != 200:
            return []
        html = r.text
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL)
    if not m:
        return []
    try:
        blob = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    entries = (
        blob.get("props", {})
        .get("pageProps", {})
        .get("entriesData", {})
        .get("entries", [])
    )
    items = []
    for i, e in enumerate(entries[:50]):
        meta = e.get("trackMetadata") or {}
        artists = ", ".join((a.get("name") or "") for a in (meta.get("artists") or []))
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=f"{meta.get('trackName', '')} — {artists}",
            url=meta.get("trackUrl"),
            image=(meta.get("displayImageUri")),
            score=float(50 - i),
            extra={"rank": i + 1},
        ))
    return items
