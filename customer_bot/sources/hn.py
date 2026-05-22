"""Hacker News source via Algolia's free search API.

Docs: https://hn.algolia.com/api — no auth required. We use
`search_by_date` so we get fresh hits rather than the all-time top.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx

from customer_bot.lead import RawLead

log = logging.getLogger("customer_bot.hn")

API_URL = "https://hn.algolia.com/api/v1/search_by_date"


async def fetch(client: httpx.AsyncClient, query: str, limit: int = 25) -> AsyncIterator[RawLead]:
    params = {
        "query": query,
        "tags": "(story,comment)",
        "hitsPerPage": str(limit),
    }
    try:
        r = await client.get(API_URL, params=params, timeout=15.0)
    except httpx.HTTPError as exc:
        log.warning("HN fetch failed for %r: %s", query, exc)
        return
    if r.status_code != 200:
        log.warning("HN returned %d for query %r", r.status_code, query)
        return
    try:
        data = r.json()
    except ValueError:
        log.warning("HN returned non-JSON for query %r", query)
        return

    for hit in (data.get("hits") or []):
        oid = hit.get("objectID")
        if not oid:
            continue
        title = hit.get("title") or hit.get("story_title") or ""
        body = hit.get("comment_text") or hit.get("story_text") or ""
        author = hit.get("author") or ""
        created = 0
        try:
            created = int(hit.get("created_at_i") or 0)
        except (TypeError, ValueError):
            pass
        yield RawLead(
            source="hn",
            source_id=f"hn:{oid}",
            url=f"https://news.ycombinator.com/item?id={oid}",
            author=author,
            title=title,
            body=body,
            posted_at=created,
            engagement=int(hit.get("points") or 0) + int(hit.get("num_comments") or 0),
            context_label="HN",
        )
