"""Reddit source — polls /r/{sub}/new.json for public listings.

Uses Reddit's free public JSON endpoint. No OAuth required for read-only
listings, but Reddit demands a descriptive User-Agent or it will rate-limit
hard. Use a distinctive UA so requests stay above the bot-blanket threshold.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx

from customer_bot.lead import RawLead

log = logging.getLogger("customer_bot.reddit")

USER_AGENT = "narve.ai-leadfinder/0.1 (contact: julian.habbig@icloud.com)"


async def fetch(client: httpx.AsyncClient, subreddit: str, limit: int = 25) -> AsyncIterator[RawLead]:
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={limit}"
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=15.0)
    except httpx.HTTPError as exc:
        log.warning("Reddit fetch failed for r/%s: %s", subreddit, exc)
        return
    if r.status_code != 200:
        log.warning("Reddit r/%s returned %d", subreddit, r.status_code)
        return
    try:
        data = r.json()
    except ValueError:
        log.warning("Reddit r/%s returned non-JSON", subreddit)
        return

    for child in (data.get("data", {}).get("children") or []):
        p = child.get("data") or {}
        # Skip stickies, ads, and removed posts.
        if p.get("stickied") or p.get("promoted") or p.get("removed_by_category"):
            continue
        post_id = p.get("id")
        if not post_id:
            continue
        permalink = p.get("permalink") or ""
        yield RawLead(
            source="reddit",
            source_id=f"reddit:{post_id}",
            url=f"https://www.reddit.com{permalink}" if permalink else (p.get("url") or ""),
            author=p.get("author") or "",
            title=p.get("title") or "",
            body=p.get("selftext") or "",
            posted_at=int(p.get("created_utc") or 0),
            engagement=int(p.get("ups") or 0) + int(p.get("num_comments") or 0),
            context_label=f"r/{subreddit}",
        )


async def fetch_comments(client: httpx.AsyncClient, subreddit: str, limit: int = 50) -> AsyncIterator[RawLead]:
    """Poll the comments stream — far higher signal than top-level posts.

    Most people don't *post* asking for tools; they *comment* in a thread
    saying "anyone know a good X". `link_title` gives the parent thread so
    drafted replies can reference context.
    """
    url = f"https://www.reddit.com/r/{subreddit}/comments.json?limit={limit}"
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=15.0)
    except httpx.HTTPError as exc:
        log.warning("Reddit comment fetch failed for r/%s: %s", subreddit, exc)
        return
    if r.status_code != 200:
        log.warning("Reddit r/%s comments returned %d", subreddit, r.status_code)
        return
    try:
        data = r.json()
    except ValueError:
        return

    for child in (data.get("data", {}).get("children") or []):
        c = child.get("data") or {}
        comment_id = c.get("id")
        if not comment_id:
            continue
        body = c.get("body") or ""
        # Reddit's [deleted] / [removed] sentinels.
        if body in ("[deleted]", "[removed]") or not body.strip():
            continue
        permalink = c.get("permalink") or ""
        yield RawLead(
            source="reddit_comment",
            source_id=f"reddit_comment:{comment_id}",
            url=f"https://www.reddit.com{permalink}" if permalink else "",
            author=c.get("author") or "",
            title=c.get("link_title") or "",   # parent thread title for context
            body=body,
            posted_at=int(c.get("created_utc") or 0),
            engagement=int(c.get("ups") or 0),
            context_label=f"r/{subreddit} (comment)",
        )
