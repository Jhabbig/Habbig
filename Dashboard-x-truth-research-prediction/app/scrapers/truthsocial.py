from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from app.config import settings
from app.models import RawPost
from app.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


class TruthSocialScraper(BaseScraper):
    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        access_token: str | None = None,
        api_base_url: str | None = None,
    ) -> None:
        self._username = username if username is not None else settings["TRUTHSOCIAL_USERNAME"]
        self._password = password if password is not None else settings["TRUTHSOCIAL_PASSWORD"]
        self._token = access_token if access_token is not None else settings["TRUTHSOCIAL_ACCESS_TOKEN"]
        self._api_base = api_base_url if api_base_url is not None else settings["TRUTHSOCIAL_API_BASE_URL"]

    def is_available(self) -> bool:
        return bool(self._username and self._password) or bool(self._token)

    async def fetch(self, keywords: list[str], limit: int = 100) -> list[RawPost]:
        if not self.is_available():
            logger.warning("TruthSocial scraper unavailable — no credentials")
            return []
        posts = await self._fetch_via_truthbrush(keywords, limit)
        if posts is not None:
            return posts
        posts = await self._fetch_via_mastodon(keywords, limit)
        if posts is not None:
            return posts
        logger.warning("TruthSocial: both methods failed")
        return []

    async def _fetch_via_truthbrush(self, keywords, limit) -> list[RawPost] | None:
        try:
            from truthbrush.api import Api as TruthApi
            def _search():
                api = TruthApi()
                results = []
                for kw in keywords:
                    try:
                        hits = api.search(kw, search_type="statuses")
                        if hits:
                            for status in list(hits)[:limit]:
                                post = self._norm_tb(status)
                                if post:
                                    results.append(post)
                    except Exception as exc:
                        logger.warning("truthbrush '%s' failed: %s", kw, exc)
                return results
            posts = await asyncio.to_thread(_search)
            logger.info("TruthSocial (truthbrush): %d posts", len(posts))
            return posts
        except ImportError:
            return None
        except Exception as exc:
            logger.warning("truthbrush failed: %s", exc)
            return None

    def _norm_tb(self, status) -> RawPost | None:
        try:
            s = status if isinstance(status, dict) else vars(status)
            account = s.get("account", {})
            post_id = str(s.get("id", ""))
            if not post_id:
                return None
            content = _strip_html(str(s.get("content", "")))
            posted_str = s.get("created_at", "")
            if isinstance(posted_str, str):
                try:
                    posted_at = datetime.fromisoformat(posted_str.replace("Z", "+00:00"))
                except ValueError:
                    posted_at = datetime.now(timezone.utc)
            elif isinstance(posted_str, datetime):
                posted_at = posted_str
            else:
                posted_at = datetime.now(timezone.utc)
            post = RawPost(
                id=f"truthsocial:{post_id}", platform="truthsocial",
                author_handle=account.get("acct", account.get("username", "unknown")),
                author_display_name=account.get("display_name", ""),
                follower_count=account.get("followers_count", 0),
                verified=account.get("verified", False),
                content=content, posted_at=posted_at, fetched_at=datetime.now(timezone.utc), engagement_json="{}",
            )
            post.engagement = {"likes": s.get("favourites_count", 0), "boosts": s.get("reblogs_count", 0), "replies": s.get("replies_count", 0)}
            return post
        except Exception:
            return None

    async def _fetch_via_mastodon(self, keywords, limit) -> list[RawPost] | None:
        if not self._token:
            return None
        try:
            from mastodon import Mastodon
            def _search():
                client = Mastodon(access_token=self._token, api_base_url=self._api_base)
                results = []
                for kw in keywords:
                    try:
                        hits = client.search_v2(kw, result_type="statuses")
                        for status in (hits.get("statuses", []) if isinstance(hits, dict) else [])[:limit]:
                            post = self._norm_masto(status)
                            if post:
                                results.append(post)
                    except Exception as exc:
                        logger.warning("Mastodon '%s' failed: %s", kw, exc)
                return results
            posts = await asyncio.to_thread(_search)
            logger.info("TruthSocial (Mastodon.py): %d posts", len(posts))
            return posts
        except ImportError:
            return None
        except Exception as exc:
            logger.warning("Mastodon.py failed: %s", exc)
            return None

    def _norm_masto(self, status: dict) -> RawPost | None:
        try:
            account = status.get("account", {})
            post_id = str(status.get("id", ""))
            if not post_id:
                return None
            content = _strip_html(str(status.get("content", "")))
            posted_at = status.get("created_at", datetime.now(timezone.utc))
            if isinstance(posted_at, str):
                try:
                    posted_at = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
                except ValueError:
                    posted_at = datetime.now(timezone.utc)
            post = RawPost(
                id=f"truthsocial:{post_id}", platform="truthsocial",
                author_handle=account.get("acct", account.get("username", "unknown")),
                author_display_name=account.get("display_name", ""),
                follower_count=account.get("followers_count", 0),
                verified=account.get("verified", False),
                content=content, posted_at=posted_at, fetched_at=datetime.now(timezone.utc), engagement_json="{}",
            )
            post.engagement = {"likes": status.get("favourites_count", 0), "boosts": status.get("reblogs_count", 0), "replies": status.get("replies_count", 0)}
            return post
        except Exception:
            return None
