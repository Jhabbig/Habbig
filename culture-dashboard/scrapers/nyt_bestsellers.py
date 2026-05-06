"""NYT bestsellers — combined print + e-book fiction & nonfiction.

Two paths:
  1. NYT Books API (`NYT_BOOKS_API_KEY` set) — clean JSON.
  2. Public bestsellers HTML page — fragile but key-free.
"""

from __future__ import annotations

import logging
import os
import re

from models import Item
from ._http import client

NAME = "nyt_bestsellers"
SECTION = "lifestyle"
REFRESH_SECONDS = 24 * 60 * 60

log = logging.getLogger(__name__)

LISTS = ["combined-print-and-e-book-fiction", "combined-print-and-e-book-nonfiction"]


async def fetch() -> list[Item]:
    key = os.environ.get("NYT_BOOKS_API_KEY")
    if key:
        return await _via_api(key)
    return await _via_html()


async def _via_api(key: str) -> list[Item]:
    items: list[Item] = []
    async with client() as c:
        for lst in LISTS:
            try:
                r = await c.get(
                    f"https://api.nytimes.com/svc/books/v3/lists/current/{lst}.json",
                    params={"api-key": key},
                )
                r.raise_for_status()
                data = r.json() or {}
            except Exception as e:  # noqa: BLE001
                log.warning("nyt %s failed: %s", lst, e)
                continue
            for b in (data.get("results") or {}).get("books", [])[:15]:
                rank = int(b.get("rank") or 99)
                items.append(Item(
                    section=SECTION,
                    source=NAME,
                    title=f"{b.get('title')} — {b.get('author')}",
                    url=b.get("amazon_product_url") or (b.get("buy_links") or [{}])[0].get("url"),
                    image=b.get("book_image"),
                    summary=(b.get("description") or "")[:300],
                    score=float(20 - rank),
                    extra={"rank": rank, "weeks_on_list": b.get("weeks_on_list"),
                           "list": lst},
                ))
    return items


async def _via_html() -> list[Item]:
    """Best-effort scrape of the public bestsellers page."""
    url = "https://www.nytimes.com/books/best-sellers/"
    async with client() as c:
        r = await c.get(url)
        if r.status_code != 200:
            return []
        html = r.text
    items: list[Item] = []
    # The page lists books inside <li> blocks with title in <h3> and author in <p>.
    block_re = re.compile(
        r'<li[^>]*itemtype="[^"]*Book"[^>]*>.*?'
        r'<h3[^>]*>([^<]+)</h3>.*?'
        r'<p[^>]*itemprop="author"[^>]*>([^<]+)</p>',
        re.DOTALL,
    )
    seen: set[str] = set()
    for m in block_re.finditer(html):
        title = m.group(1).strip()
        author = m.group(2).strip()
        if title in seen:
            continue
        seen.add(title)
        items.append(Item(
            section=SECTION,
            source=NAME,
            title=f"{title} — {author}",
            score=float(20 - len(items)),
        ))
        if len(items) >= 30:
            break
    return items
