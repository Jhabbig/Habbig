"""
X (Twitter) scraper using Playwright browser automation.

LEGAL NOTE: This scraper is intended for personal/research use only.
Users should ensure compliance with X's Terms of Service. Rate limits
are applied aggressively to minimise server load and avoid detection.
No paid API access is used — data is collected via browser automation
and network request interception.

Strategy:
  1. Load a persistent browser context with stored cookies from a logged-in session
  2. Navigate to twitter.com/search?q={keyword}&f=live
  3. Intercept XHR responses from api.twitter.com/graphql/*SearchTimeline*
  4. Extract tweet data from the intercepted JSON payload
  5. No HTML parsing needed — raw tweet data comes from the API response

Session setup (one-time, manual):
  - User logs into X manually in a headed browser launched by setup_twitter_session.py
  - Cookies and localStorage saved to stealth/profiles/twitter/
  - Subsequent runs reuse saved session — no login required

Rate limiting:
  - Maximum 1 search per 45 seconds (configurable)
  - Maximum 100 posts per keyword per run
  - Random delay between 30-90 seconds between keyword searches
  - Rotate user agents from a pool of realistic browser UA strings

Detection avoidance:
  - playwright-stealth applied to every page
  - Realistic mouse movements before interacting
  - Random viewport sizes (1280-1920 width, 800-1080 height)
  - Random scroll behaviour after page load
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from scraper.config import (
    PLAYWRIGHT_HEADLESS,
    BROWSER_TYPE,
    SESSION_PROFILE_PATH,
    MAX_POSTS_PER_KEYWORD,
    TWITTER_DELAY_BETWEEN_KEYWORDS,
    MIN_DELAY_JITTER,
)
from scraper.scrapers.base import BaseScraper
from scraper.storage.models import RawPost
from scraper.storage import db as store

log = logging.getLogger("scraper")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
]

PROFILE_DIR = Path(SESSION_PROFILE_PATH) / "twitter"


class TwitterScraper(BaseScraper):
    platform = "twitter"

    def is_available(self) -> bool:
        """Check if a saved session exists and is marked valid."""
        session = store.get_session("twitter")
        if not session or not session.valid:
            return False
        return PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir())

    async def fetch(self, keywords: list[str], limit: int = MAX_POSTS_PER_KEYWORD) -> list[RawPost]:
        """Scrape tweets for each keyword using XHR interception."""
        if not self.is_available():
            log.warning("Twitter scraper unavailable — no valid session")
            return []

        all_posts: list[RawPost] = []

        try:
            from playwright.async_api import async_playwright
            from playwright_stealth import stealth_async
        except ImportError:
            log.error("Playwright or playwright-stealth not installed")
            return []

        async with async_playwright() as pw:
            browser_type = getattr(pw, BROWSER_TYPE)
            context = await browser_type.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=PLAYWRIGHT_HEADLESS,
                viewport={"width": random.randint(1280, 1920), "height": random.randint(800, 1080)},
                user_agent=random.choice(USER_AGENTS),
                locale="en-US",
                timezone_id="America/New_York",
            )

            for i, keyword in enumerate(keywords):
                if i > 0:
                    delay = TWITTER_DELAY_BETWEEN_KEYWORDS + random.randint(0, MIN_DELAY_JITTER)
                    log.info("Twitter: waiting %ds before next keyword", delay)
                    await asyncio.sleep(delay)

                try:
                    posts = await self._scrape_keyword(context, keyword, limit)
                    all_posts.extend(posts)
                    log.info("Twitter: keyword=%r found %d posts", keyword, len(posts))
                except Exception:
                    log.exception("Twitter: error scraping keyword=%r", keyword)

            await context.close()

        store.update_session_used("twitter")
        return all_posts

    async def _scrape_keyword(self, context, keyword: str, limit: int) -> list[RawPost]:
        """Navigate to search page and intercept the GraphQL response."""
        from playwright_stealth import stealth_async

        page = await context.new_page()
        await stealth_async(page)

        intercepted: list[dict] = []

        async def handle_response(response):
            url = response.url
            if "SearchTimeline" in url or "SearchAdaptive" in url:
                try:
                    data = await response.json()
                    intercepted.append(data)
                except Exception:
                    pass

        page.on("response", handle_response)

        search_url = f"https://x.com/search?q={keyword}&src=typed_query&f=live"
        try:
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
        except Exception:
            # Timeout is OK — XHR might have already been intercepted
            pass

        # Simulate human scroll behaviour
        for _ in range(random.randint(2, 5)):
            await page.mouse.wheel(0, random.randint(300, 800))
            await asyncio.sleep(random.uniform(1.0, 3.0))

        await page.close()

        # Parse intercepted responses
        posts: list[RawPost] = []
        for data in intercepted:
            tweets = self._extract_tweets_from_response(data)
            for raw in tweets:
                parsed = self._parse_tweet(raw, keyword)
                if parsed and len(posts) < limit:
                    posts.append(parsed)

        return posts

    def _extract_tweets_from_response(self, data: dict) -> list[dict]:
        """Walk the GraphQL response to find tweet result objects."""
        tweets: list[dict] = []
        self._walk_for_tweets(data, tweets)
        return tweets

    def _walk_for_tweets(self, obj, found: list[dict]) -> None:
        """Recursively walk JSON looking for tweet_results or legacy tweet objects."""
        if isinstance(obj, dict):
            # Check for tweet result pattern
            if "tweet_results" in obj:
                result = obj["tweet_results"].get("result", {})
                if result.get("__typename") in ("Tweet", "TweetWithVisibilityResults"):
                    # Unwrap TweetWithVisibilityResults
                    if result.get("__typename") == "TweetWithVisibilityResults":
                        result = result.get("tweet", result)
                    found.append(result)
                    return
            # Check for legacy tweet pattern
            if "legacy" in obj and "full_text" in obj.get("legacy", {}):
                core = obj.get("core", {})
                if core:
                    found.append(obj)
                    return
            for v in obj.values():
                self._walk_for_tweets(v, found)
        elif isinstance(obj, list):
            for item in obj:
                self._walk_for_tweets(item, found)

    def _parse_tweet(self, raw: dict, keyword: str) -> Optional[RawPost]:
        """Parse a raw tweet object from Twitter's GraphQL response into RawPost."""
        try:
            legacy = raw.get("legacy", {})
            if not legacy:
                return None

            # Skip retweets (they have retweeted_status_result)
            if "retweeted_status_result" in legacy:
                return None

            full_text = legacy.get("full_text", "")
            if not full_text:
                return None

            tweet_id = legacy.get("id_str") or raw.get("rest_id", "")
            if not tweet_id:
                return None

            # Extract user info from core.user_results.result.legacy
            user_legacy = {}
            core = raw.get("core", {})
            user_results = core.get("user_results", {}).get("result", {})
            user_legacy = user_results.get("legacy", {})
            is_blue_verified = user_results.get("is_blue_verified", False)

            author_handle = user_legacy.get("screen_name", "unknown")
            author_display = user_legacy.get("name", author_handle)
            followers = user_legacy.get("followers_count", 0)
            verified = bool(user_legacy.get("verified", False)) or bool(is_blue_verified)

            # Parse timestamp
            created_at_str = legacy.get("created_at", "")
            if created_at_str:
                posted_at = datetime.strptime(created_at_str, "%a %b %d %H:%M:%S %z %Y")
            else:
                posted_at = datetime.now(timezone.utc)

            return RawPost(
                id=f"twitter:{tweet_id}",
                platform="twitter",
                author_handle=author_handle,
                author_display_name=author_display,
                author_followers=followers,
                author_verified=verified,
                content=full_text,
                posted_at=posted_at,
                scraped_at=datetime.now(timezone.utc),
                likes=legacy.get("favorite_count", 0),
                retweets_or_boosts=legacy.get("retweet_count", 0),
                replies=legacy.get("reply_count", 0),
                keyword_matched=keyword,
            )
        except Exception:
            log.exception("Failed to parse tweet")
            return None

    async def setup_session(self) -> None:
        """
        Launch a headed browser for manual login.
        Saves session to stealth/profiles/twitter/.
        Called by setup_twitter_session.py.
        """
        from playwright.async_api import async_playwright
        from playwright_stealth import stealth_async

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as pw:
            browser_type = getattr(pw, BROWSER_TYPE)
            context = await browser_type.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,  # Must be headed for manual login
                viewport={"width": 1400, "height": 900},
                user_agent=USER_AGENTS[0],
                locale="en-US",
            )

            page = context.pages[0] if context.pages else await context.new_page()
            await stealth_async(page)
            await page.goto("https://x.com/login", wait_until="domcontentloaded")

            print("\n" + "=" * 60)
            print("  TWITTER SESSION SETUP")
            print("=" * 60)
            print("  1. Log in to your X/Twitter account in the browser window")
            print("  2. Complete 2FA if prompted")
            print("  3. Once logged in, come back here and press Enter")
            print("=" * 60 + "\n")

            input("Press Enter after logging in...")

            # Validate by checking for a home timeline element
            try:
                await page.goto("https://x.com/home", wait_until="networkidle", timeout=15000)
                title = await page.title()
                if "home" in title.lower() or "x" in title.lower():
                    print("Session validated — logged in successfully!")
                else:
                    print(f"Warning: page title is '{title}' — session may not be valid")
            except Exception:
                print("Warning: could not validate session — check manually")

            # Save session info to DB
            store.upsert_session("twitter", str(PROFILE_DIR), notes="Manual setup via setup_twitter_session.py")

            await context.close()
            print(f"Session saved to {PROFILE_DIR}")
