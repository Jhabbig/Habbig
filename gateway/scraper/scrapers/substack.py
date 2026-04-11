"""Substack RSS scraper (F11).

Fetches recent posts from prediction-focused Substack newsletters via their
public RSS feeds. No auth required — all feeds are publicly accessible.

Long-form text is passed through Claude extraction (F10) for prediction
identification. Each post becomes a RawPost that flows through the normal
scraper pipeline.

Source handles: "substack:{publication_slug}"
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from scraper.scrapers.base import BaseScraper
from scraper.storage.models import RawPost

log = logging.getLogger("scraper.substack")

# Default feeds — prediction-focused newsletters.
# Operators can override via SUBSTACK_FEEDS env var (comma-separated URLs).
DEFAULT_FEEDS = [
    # Nate Silver's Silver Bulletin
    "https://www.natesilver.net/feed",
    # Add more feeds here as they're discovered
]


def _get_feeds() -> list[str]:
    """Return the list of Substack RSS feed URLs to scrape."""
    env_feeds = os.environ.get("SUBSTACK_FEEDS", "").strip()
    if env_feeds:
        return [f.strip() for f in env_feeds.split(",") if f.strip()]
    return DEFAULT_FEEDS


class SubstackScraper(BaseScraper):
    """Scraper for Substack newsletters via RSS feeds."""

    platform = "substack"

    async def fetch(self, keywords: list[str], limit: int = 100) -> list[RawPost]:
        posts: list[RawPost] = []
        seen_urls: set[str] = set()
        feeds = _get_feeds()

        for feed_url in feeds:
            try:
                entries = await self._fetch_feed(feed_url)
                pub_slug = self._extract_slug(feed_url)

                for entry in entries:
                    url = entry.get("link", "")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    title = entry.get("title", "")
                    # RSS content may be in different fields depending on the feed
                    content_raw = (
                        entry.get("content", [{}])[0].get("value", "")
                        if entry.get("content")
                        else entry.get("summary", "")
                    )
                    # Strip HTML tags for plain text (rough)
                    import re
                    content_text = re.sub(r"<[^>]+>", " ", content_raw)
                    content_text = re.sub(r"\s+", " ", content_text).strip()[:2000]

                    full_content = f"{title}\n\n{content_text}"

                    # Keyword filter: only include posts matching at least one keyword
                    lower_content = full_content.lower()
                    matched_kw = ""
                    for kw in keywords:
                        if kw.lower() in lower_content:
                            matched_kw = kw
                            break
                    if not matched_kw and keywords:
                        continue

                    # Parse publish date
                    pub_date_str = entry.get("published", "") or entry.get("updated", "")
                    try:
                        from email.utils import parsedate_to_datetime
                        posted_at = parsedate_to_datetime(pub_date_str)
                    except (ValueError, TypeError):
                        posted_at = datetime.now(timezone.utc)

                    author_name = entry.get("author", pub_slug)

                    post = RawPost(
                        id=f"substack:{pub_slug}:{url}",
                        platform="substack",
                        author_handle=f"substack:{pub_slug}",
                        author_display_name=author_name,
                        author_followers=0,
                        author_verified=True,
                        content=full_content,
                        posted_at=posted_at,
                        scraped_at=datetime.now(timezone.utc),
                        likes=0,
                        retweets_or_boosts=0,
                        replies=0,
                        keyword_matched=matched_kw,
                    )
                    posts.append(post)

            except Exception as e:
                log.exception("Substack fetch error for %s: %s", feed_url, e)

        log.info("Substack: fetched %d posts from %d feeds", len(posts), len(feeds))
        return posts[:limit]

    async def _fetch_feed(self, feed_url: str) -> list[dict]:
        """Fetch and parse an RSS feed. Returns list of entry dicts."""
        try:
            import feedparser
        except ImportError:
            log.warning("feedparser not installed, skipping Substack scraping")
            return []

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(feed_url)
            if resp.status_code != 200:
                log.warning("Substack feed %s returned %d", feed_url, resp.status_code)
                return []

        feed = feedparser.parse(resp.text)
        return feed.entries[:20]  # last 20 entries

    def _extract_slug(self, feed_url: str) -> str:
        """Extract a publication slug from the feed URL."""
        # https://www.natesilver.net/feed → natesilver
        from urllib.parse import urlparse
        parsed = urlparse(feed_url)
        hostname = parsed.hostname or ""
        # Remove www. prefix and .substack.com suffix
        slug = hostname.replace("www.", "").replace(".substack.com", "")
        # Remove TLD
        parts = slug.split(".")
        return parts[0] if parts else slug

    def is_available(self) -> bool:
        return True  # RSS feeds are always available

    async def health_check(self) -> dict:
        feeds = _get_feeds()
        if not feeds:
            return {"available": False, "error": "no feeds configured"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(feeds[0])
                return {"available": resp.status_code == 200, "feeds": len(feeds)}
        except Exception as e:
            return {"available": False, "error": str(e)}
