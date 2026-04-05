from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings, yaml_config
from app.models import RawPost
from app.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class TwitterScraper(BaseScraper):
    def __init__(self) -> None:
        self._bearer_token: str = settings["TWITTER_BEARER_TOKEN"]
        self._monthly_quota: int = settings["TWITTER_MONTHLY_QUOTA"]
        self._client = None
        if self._bearer_token:
            try:
                import tweepy
                self._client = tweepy.Client(bearer_token=self._bearer_token, wait_on_rate_limit=True)
            except Exception as exc:
                logger.warning("Could not initialise Tweepy client: %s", exc)

    def _current_month_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    async def _get_quota_used(self) -> int:
        from sqlmodel import select
        from app.db import AsyncSession, engine
        from app.models import MonthlyQuota

        month_key = self._current_month_key()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            stmt = select(MonthlyQuota).where(MonthlyQuota.platform == "twitter", MonthlyQuota.year_month == month_key)
            result = await session.exec(stmt)
            row = result.first()
            return row.tweets_read if row else 0

    async def _increment_quota(self, count: int) -> None:
        from sqlmodel import select
        from app.db import AsyncSession, engine
        from app.models import MonthlyQuota

        month_key = self._current_month_key()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            stmt = select(MonthlyQuota).where(MonthlyQuota.platform == "twitter", MonthlyQuota.year_month == month_key)
            result = await session.exec(stmt)
            row = result.first()
            if row:
                row.tweets_read += count
                row.last_updated = datetime.now(timezone.utc)
                session.add(row)
            else:
                session.add(MonthlyQuota(platform="twitter", year_month=month_key, tweets_read=count, last_updated=datetime.now(timezone.utc)))
            await session.commit()

    def is_available(self) -> bool:
        return bool(self._client)

    async def fetch(self, keywords: list[str], limit: int = 100) -> list[RawPost]:
        if not self._client:
            logger.warning("Twitter scraper unavailable — no bearer token")
            return []

        quota_used = await self._get_quota_used()
        quota_cfg = yaml_config.get("quota", {})
        if quota_used >= self._monthly_quota * quota_cfg.get("twitter_hard_stop_pct", 1.0):
            logger.warning("Twitter monthly quota exhausted (%d/%d)", quota_used, self._monthly_quota)
            return []

        effective_limit = min(limit, 100)
        if quota_used >= self._monthly_quota * quota_cfg.get("twitter_warn_pct", 0.80):
            effective_limit = max(10, effective_limit // 2)

        query = " OR ".join(f'"{kw}"' for kw in keywords) + " -is:retweet lang:en"
        posts: list[RawPost] = []

        try:
            response = await asyncio.to_thread(
                self._client.search_recent_tweets, query=query, max_results=effective_limit,
                expansions="author_id", tweet_fields=["created_at", "public_metrics", "author_id"],
                user_fields=["public_metrics", "verified", "username", "name"],
            )
            if response.data is None:
                return []

            users_map: dict = {}
            if response.includes and "users" in response.includes:
                users_map = {u.id: u for u in response.includes["users"]}

            for tweet in response.data:
                author = users_map.get(tweet.author_id)
                metrics = tweet.public_metrics or {}
                author_metrics = (author.public_metrics or {}) if author else {}
                post = RawPost(
                    id=f"twitter:{tweet.id}", platform="twitter",
                    author_handle=author.username if author else str(tweet.author_id),
                    author_display_name=author.name if author else "",
                    follower_count=author_metrics.get("followers_count", 0),
                    verified=getattr(author, "verified", False) if author else False,
                    content=tweet.text, posted_at=tweet.created_at or datetime.now(timezone.utc),
                    fetched_at=datetime.now(timezone.utc), engagement_json="{}",
                )
                post.engagement = {"likes": metrics.get("like_count", 0), "retweets": metrics.get("retweet_count", 0), "replies": metrics.get("reply_count", 0)}
                posts.append(post)

            await self._increment_quota(len(posts))
            logger.info("Twitter: fetched %d posts", len(posts))
        except Exception as exc:
            logger.error("Twitter scrape failed: %s", exc)
        return posts
