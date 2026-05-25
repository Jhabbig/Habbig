"""Reddit scraper.

Pulls recent text posts from configured subreddits via Reddit's public JSON
endpoint (no OAuth needed for read-only access to public subs). Filters posts
to those containing at least one prediction keyword, since that's where signal
density is highest.

We keep the API call surface narrow: one GET per subreddit per cycle, capped at
`limit_per_source` posts. Reddit's anti-bot heuristics tolerate this if we send
a sane User-Agent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import httpx

from app.config import yaml_config
from app.models import RawPost
from app.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

_USER_AGENT = "narve.ai-truth-research/1.0 (prediction-market signal harvesting)"
_DEFAULT_SUBREDDITS = [
    "PredictionMarkets",
    "politics",
    "sportsbook",
    "CryptoCurrency",
    "geopolitics",
]


class RedditScraper(BaseScraper):
    def __init__(self, subreddits: Iterable[str] | None = None) -> None:
        # An explicit list (even empty) overrides everything — caller intent wins.
        # Only fall back to config / defaults when no list is passed.
        if subreddits is not None:
            self._subreddits = list(subreddits)
        else:
            cfg = yaml_config.get("scraping", {}).get("reddit", {})
            configured = cfg.get("subreddits")
            self._subreddits = list(configured) if configured else list(_DEFAULT_SUBREDDITS)

    def is_available(self) -> bool:
        # Reddit's public JSON works without auth. We can always scrape unless
        # the operator has explicitly disabled it via empty subreddit list.
        return bool(self._subreddits)

    async def fetch(self, keywords: list[str], limit: int = 100) -> list[RawPost]:
        if not self._subreddits:
            return []
        per_sub = max(1, min(100, limit // max(1, len(self._subreddits))))
        kw_lower = [k.lower() for k in keywords]
        posts: list[RawPost] = []
        async with httpx.AsyncClient(
            timeout=20,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            for sub in self._subreddits:
                try:
                    resp = await client.get(
                        f"https://www.reddit.com/r/{sub}/new.json",
                        params={"limit": per_sub, "raw_json": 1},
                    )
                    if resp.status_code != 200:
                        logger.info("Reddit r/%s returned %s; skipping", sub, resp.status_code)
                        continue
                    data = resp.json()
                except Exception as exc:
                    logger.warning("Reddit r/%s fetch failed: %s", sub, exc)
                    continue

                for child in (data.get("data", {}).get("children", []) or []):
                    post = self._normalize(child.get("data", {}) or {}, kw_lower)
                    if post is not None:
                        posts.append(post)
        logger.info("Reddit: fetched %d posts across %d subs", len(posts), len(self._subreddits))
        return posts

    @staticmethod
    def _normalize(item: dict, keywords_lower: list[str]) -> RawPost | None:
        """Convert one Reddit listing entry to a RawPost. Skips low-signal items."""
        kind = item.get("kind") or ""
        if item.get("over_18") or item.get("stickied"):
            return None
        title = (item.get("title") or "").strip()
        selftext = (item.get("selftext") or "").strip()
        content = f"{title}\n\n{selftext}".strip() if selftext else title
        if len(content) < 30:
            return None  # too short to contain a useful prediction

        lower = content.lower()
        if not any(kw in lower for kw in keywords_lower):
            return None  # no prediction-style language; not worth the LLM round-trip

        post_id = item.get("id") or ""
        if not post_id:
            return None
        author = item.get("author") or "unknown"
        if author in ("[deleted]", "AutoModerator"):
            return None

        posted_ts = item.get("created_utc")
        posted_at = (
            datetime.fromtimestamp(float(posted_ts), tz=timezone.utc)
            if posted_ts else datetime.now(timezone.utc)
        )
        post = RawPost(
            id=f"reddit:{post_id}",
            platform="reddit",
            author_handle=author,
            author_display_name=author,
            # Don't proxy subreddit_subscribers — every poster from r/politics
            # would otherwise read as having ~8M "followers" and inflate the
            # source's volume_component in the credibility engine. Reddit's
            # public listing JSON doesn't expose author karma, so leave it 0.
            follower_count=0,
            verified=bool(item.get("author_premium")),
            content=content[:4000],
            posted_at=posted_at,
            fetched_at=datetime.now(timezone.utc),
            engagement_json="{}",
        )
        post.engagement = {
            "score": int(item.get("score") or 0),
            "ups": int(item.get("ups") or 0),
            "comments": int(item.get("num_comments") or 0),
        }
        return post
