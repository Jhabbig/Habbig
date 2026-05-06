"""Steam — current top games by player count."""

from __future__ import annotations

import logging
import re

from models import Item
from ._http import client

NAME = "steam_top_played"
SECTION = "entertainment"
REFRESH_SECONDS = 6 * 60 * 60

log = logging.getLogger(__name__)

URL = "https://store.steampowered.com/charts/mostplayed"


async def fetch() -> list[Item]:
    async with client() as c:
        r = await c.get(URL)
        if r.status_code != 200:
            return []
        html = r.text
    items: list[Item] = []
    # Each row is a <a class="..._RowLink_..." href="/app/<id>/<slug>/">…<div class="..._Name">Name</div>…<td>peak</td>
    for m in re.finditer(
        r'href="(/app/(\d+)/[^"]+)"[^>]*>.*?_Name[^>]*>([^<]+)<.*?(\d[\d,]*)\s*</td>\s*<td[^>]*>\s*(\d[\d,]*)',
        html, re.DOTALL,
    ):
        if len(items) >= 20:
            break
        href, _appid, name, current, _peak = m.groups()
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=name.strip(),
            url=f"https://store.steampowered.com{href}",
            score=float(int(current.replace(",", ""))),
            extra={"current_players": int(current.replace(",", ""))},
        ))
    return items
