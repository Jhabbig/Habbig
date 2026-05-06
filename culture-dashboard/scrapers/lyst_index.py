"""Lyst Index — quarterly fashion brands & products ranking.

Lyst publishes the index at https://www.lyst.com/data/the-lyst-index/. The
data updates quarterly so a long refresh is fine. We extract the brand
table (rank → brand) from the rendered HTML.
"""

from __future__ import annotations

import logging
import re

from models import Item
from ._http import client

NAME = "lyst_index"
SECTION = "lifestyle"
REFRESH_SECONDS = 7 * 24 * 60 * 60

log = logging.getLogger(__name__)

URL = "https://www.lyst.com/data/the-lyst-index/"


async def fetch() -> list[Item]:
    async with client() as c:
        r = await c.get(URL)
        if r.status_code != 200:
            return []
        html = r.text
    items: list[Item] = []
    # The table cells follow <td>1</td><td>Brand Name</td>… pattern in the
    # rendered page. Pull the first ~20 ranks.
    rows = re.findall(
        r'<td[^>]*>\s*(\d{1,2})\s*</td>\s*<td[^>]*>\s*([^<\n]{2,60}?)\s*</td>',
        html,
    )
    seen: set[str] = set()
    for rank_s, brand in rows:
        rank = int(rank_s)
        brand = brand.strip()
        if not brand or brand in seen or rank > 20:
            continue
        seen.add(brand)
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=brand,
            url=f"https://www.lyst.com/brands/{brand.lower().replace(' ', '-')}/",
            score=float(21 - rank),
            extra={"rank": rank},
        ))
    items.sort(key=lambda i: i.score, reverse=True)
    return items[:20]
