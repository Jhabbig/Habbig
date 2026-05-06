"""Weekend box office via The Numbers public weekend chart.

The Numbers (the-numbers.com) publishes an HTML weekend chart that is far
more parser-friendly than Box Office Mojo's JS-rendered page.
"""

from __future__ import annotations

import logging
import re

from models import Item
from ._http import client

NAME = "box_office"
SECTION = "entertainment"
REFRESH_SECONDS = 12 * 60 * 60

log = logging.getLogger(__name__)

URL = "https://www.the-numbers.com/box-office-chart/weekend"

ROW_RE = re.compile(
    r'<td[^>]*>\s*(?P<rank>\d+)\s*</td>'
    r'.*?<a[^>]+href="(?P<href>/movie/[^"]+)"[^>]*>(?P<title>[^<]+)</a>'
    r'.*?\$([\d,]+)',
    re.DOTALL,
)


async def fetch() -> list[Item]:
    async with client() as c:
        r = await c.get(URL)
        r.raise_for_status()
        html = r.text
    items: list[Item] = []
    for m in ROW_RE.finditer(html):
        if len(items) >= 20:
            break
        gross = int(m.group(4).replace(",", ""))
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=m.group("title").strip(),
            url=f"https://www.the-numbers.com{m.group('href')}",
            score=float(gross),
            extra={"rank": int(m.group("rank"))},
        ))
    return items
