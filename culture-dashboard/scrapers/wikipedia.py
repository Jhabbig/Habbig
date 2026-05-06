"""Wikipedia top pageviews — yesterday's most-read articles.

Pageviews API is free and unauthenticated. Yesterday is the safest day to
ask for: today's data is incomplete and lags behind.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from models import Item
from ._http import client

NAME = "wikipedia_top"
SECTION = "attention"
REFRESH_SECONDS = 6 * 60 * 60

log = logging.getLogger(__name__)


async def fetch() -> list[Item]:
    d = date.today() - timedelta(days=1)
    url = (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
        f"en.wikipedia.org/all-access/{d.year}/{d.month:02d}/{d.day:02d}"
    )
    async with client() as c:
        r = await c.get(url)
        r.raise_for_status()
        data = r.json() or {}
    out: list[Item] = []
    for art in (data.get("items") or [{}])[0].get("articles", [])[:50]:
        title = art.get("article", "").replace("_", " ")
        # Skip the boilerplate that always tops these lists.
        if title in ("Main Page", "Special:Search", "-", ""):
            continue
        out.append(Item(
            section=SECTION,
            source=NAME,
            title=title,
            url=f"https://en.wikipedia.org/wiki/{art.get('article', '')}",
            score=float(art.get("views") or 0),
            extra={"rank": art.get("rank")},
        ))
    return out[:30]
