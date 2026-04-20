"""
Reddit source — public /new.json polling across a static sub list.

No auth required. Polite User-Agent + sub-to-sub spacing keeps us well under
Reddit's unauthenticated rate limits (~60 req/min). Each sub is fetched
independently; one bad sub (429/403) triggers per-sub exponential backoff but
does NOT stop the loop from fetching other subs.

Why sub-by-sub /new and not global search: Reddit keyword search is rate-limited
harder, ranks by relevance not recency, and misses signal from subs like
r/mildlyinfuriating that don't trigger on keywords. Polling /new is the clean,
legal, reliable path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

import config
from sources.base import SourceBase, RawPost

log = logging.getLogger("annoyance.sources.reddit")


# Per-sub exponential backoff state. Key = sub name, value = (ready_at_monotonic, fail_count)
_backoff: dict[str, tuple[float, int]] = {}


def _is_sub_in_backoff(sub: str) -> bool:
    entry = _backoff.get(sub)
    if not entry:
        return False
    ready_at, _ = entry
    return time.monotonic() < ready_at


def _record_sub_failure(sub: str) -> None:
    _, fail_count = _backoff.get(sub, (0.0, 0))
    fail_count += 1
    # 60s, 120s, 240s, 480s, ... capped at 1h
    delay = min(60 * (2 ** (fail_count - 1)), 3600)
    _backoff[sub] = (time.monotonic() + delay, fail_count)
    log.warning("reddit sub %s in backoff for %ds (failures=%d)", sub, delay, fail_count)


def _record_sub_success(sub: str) -> None:
    if sub in _backoff:
        del _backoff[sub]


class RedditSource(SourceBase):
    name = "reddit"

    async def fetch(self) -> list[RawPost]:
        all_posts: list[RawPost] = []
        headers = {"User-Agent": config.REDDIT_USER_AGENT}

        async with httpx.AsyncClient(
            headers=headers, timeout=15.0, follow_redirects=True,
        ) as client:
            for sub in config.REDDIT_SUBS:
                if _is_sub_in_backoff(sub):
                    continue

                try:
                    posts = await self._fetch_sub(client, sub)
                    all_posts.extend(posts)
                    _record_sub_success(sub)
                    log.info("reddit r/%s: %d posts", sub, len(posts))
                except httpx.HTTPStatusError as e:
                    status = e.response.status_code if e.response else 0
                    if status in (429, 403):
                        _record_sub_failure(sub)
                    else:
                        log.warning("reddit r/%s HTTP %s: %s", sub, status, e)
                except Exception:
                    log.exception("reddit r/%s unexpected error", sub)

                await asyncio.sleep(config.REDDIT_REQUEST_SPACING_SECONDS)

        return all_posts

    async def _fetch_sub(self, client: httpx.AsyncClient, sub: str) -> list[RawPost]:
        url = f"https://www.reddit.com/r/{sub}/new.json?limit={config.REDDIT_POSTS_PER_SUB}"
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

        children = data.get("data", {}).get("children", [])
        out: list[RawPost] = []
        for child in children:
            d = child.get("data", {}) or {}
            parsed = self._parse(d, sub)
            if parsed:
                out.append(parsed)
        return out

    def _parse(self, d: dict, sub: str) -> Optional[RawPost]:
        native_id = d.get("id")
        if not native_id:
            return None

        # Reddit posts have title + selftext. We concatenate because the title is
        # often where the complaint lives ("United just cancelled my flight AGAIN").
        title = (d.get("title") or "").strip()
        body = (d.get("selftext") or "").strip()
        content = f"{title}\n{body}".strip() if body else title
        if not content:
            return None

        # posted_at is unix seconds in Reddit API
        created_utc = d.get("created_utc")
        if created_utc is None:
            return None
        posted_at = datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat()

        ups = int(d.get("ups") or 0)
        num_comments = int(d.get("num_comments") or 0)
        engagement = ups + num_comments

        permalink = d.get("permalink") or ""
        url = f"https://www.reddit.com{permalink}" if permalink else None
        author = d.get("author") or None

        return RawPost(
            id=f"reddit:{native_id}",
            source="reddit",
            source_channel=f"r/{sub}",
            author=author,
            content=content[:4000],  # guard against massive selftext
            posted_at=posted_at,
            url=url,
            engagement=engagement,
            keyword=None,
        )
