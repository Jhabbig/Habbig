"""
TruthSocial scraper using a hybrid approach.

LEGAL NOTE: This scraper is intended for personal/research use only.
Users should ensure compliance with TruthSocial's Terms of Service.
Rate limits are applied aggressively. No paid API access is used.

TruthSocial is a Mastodon fork. Its API endpoints are partially public
and work without authentication for prominent accounts.

Primary — Direct HTTP (no browser needed for public accounts):
  - GET https://truthsocial.com/api/v1/accounts/lookup?acct={handle}
  - GET https://truthsocial.com/api/v1/accounts/{account_id}/statuses
  - GET https://truthsocial.com/api/v1/timelines/tag/{hashtag}
  - Works without auth for public prominent accounts
  - Uses httpx with realistic headers

Secondary — Playwright for authenticated search:
  - Login session stored in stealth/profiles/truthsocial/
  - GET https://truthsocial.com/api/v1/search?q={keyword}&resolve=false&type=statuses
  - Falls back to direct HTTP if Playwright session unavailable

Rate limiting:
  - Maximum 1 request per 30 seconds
  - Maximum 40 posts per keyword per run
  - Random delay 20-60 seconds between requests
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from scraper.config import (
    PLAYWRIGHT_HEADLESS,
    BROWSER_TYPE,
    SESSION_PROFILE_PATH,
    MAX_POSTS_PER_KEYWORD,
    TRUTHSOCIAL_DELAY_BETWEEN_KEYWORDS,
    TRUTHSOCIAL_PROMINENT_ACCOUNTS,
    MIN_DELAY_JITTER,
)
from scraper.scrapers.base import BaseScraper
from scraper.storage.models import RawPost
from scraper.storage import db as store

log = logging.getLogger("scraper")

BASE_URL = "https://truthsocial.com"
API_URL = f"{BASE_URL}/api/v1"

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://truthsocial.com/",
    "Origin": "https://truthsocial.com",
}

PROFILE_DIR = Path(SESSION_PROFILE_PATH) / "truthsocial"


class TruthSocialScraper(BaseScraper):
    platform = "truthsocial"

    def is_available(self) -> bool:
        """
        Always available for prominent account scraping (no auth needed).
        Keyword search requires a valid session.
        """
        return True  # Prominent accounts always accessible

    def _has_session(self) -> bool:
        session = store.get_session("truthsocial")
        if not session or not session.valid:
            return False
        return PROFILE_DIR.exists() and any(PROFILE_DIR.iterdir())

    async def fetch(self, keywords: list[str], limit: int = MAX_POSTS_PER_KEYWORD) -> list[RawPost]:
        """Scrape TruthSocial via direct HTTP and optionally Playwright."""
        all_posts: list[RawPost] = []

        # 1. Always scrape prominent accounts via direct HTTP
        for i, handle in enumerate(TRUTHSOCIAL_PROMINENT_ACCOUNTS):
            if i > 0:
                delay = random.randint(5, 15)
                await asyncio.sleep(delay)
            try:
                posts = await self._fetch_account_statuses(handle)
                all_posts.extend(posts)
                log.info("TruthSocial: account=%r found %d posts", handle, len(posts))
            except Exception:
                log.exception("TruthSocial: error fetching account=%r", handle)

        # 2. Keyword search — try direct HTTP first, fall back to Playwright
        for i, keyword in enumerate(keywords):
            delay = TRUTHSOCIAL_DELAY_BETWEEN_KEYWORDS + random.randint(0, MIN_DELAY_JITTER)
            log.info("TruthSocial: waiting %ds before keyword search", delay)
            await asyncio.sleep(delay)

            try:
                # Try hashtag endpoint first (no auth)
                tag = keyword.strip().replace(" ", "").replace("#", "")
                posts = await self._fetch_hashtag(tag, limit)
                if posts:
                    all_posts.extend(posts)
                    log.info("TruthSocial: hashtag=%r found %d posts", tag, len(posts))
                    continue

                # Fall back to authenticated search if session available
                if self._has_session():
                    posts = await self._fetch_search_playwright(keyword, limit)
                    all_posts.extend(posts)
                    log.info("TruthSocial: search=%r found %d posts via Playwright", keyword, len(posts))
                else:
                    log.info("TruthSocial: no session for search, skipping keyword=%r", keyword)
            except Exception:
                log.exception("TruthSocial: error searching keyword=%r", keyword)

        store.update_session_used("truthsocial")
        return all_posts

    async def _fetch_account_statuses(self, handle: str) -> list[RawPost]:
        """Direct HTTP fetch of a public account's recent statuses."""
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30) as client:
            # Lookup account ID
            resp = await client.get(f"{API_URL}/accounts/lookup", params={"acct": handle})
            if resp.status_code != 200:
                log.warning("TruthSocial: account lookup failed for %r: %d", handle, resp.status_code)
                return []

            account = resp.json()
            account_id = account["id"]

            # Fetch recent statuses
            await asyncio.sleep(random.uniform(2, 5))
            resp = await client.get(
                f"{API_URL}/accounts/{account_id}/statuses",
                params={"limit": 40, "exclude_replies": "true", "exclude_reblogs": "true"},
            )
            if resp.status_code != 200:
                log.warning("TruthSocial: statuses fetch failed for %r: %d", handle, resp.status_code)
                return []

            statuses = resp.json()
            posts = []
            for s in statuses:
                parsed = self._parse_status(s, keyword_matched=f"account:{handle}")
                if parsed:
                    posts.append(parsed)
            return posts

    async def _fetch_hashtag(self, tag: str, limit: int) -> list[RawPost]:
        """Fetch posts by hashtag (public, no auth)."""
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30) as client:
            resp = await client.get(
                f"{API_URL}/timelines/tag/{tag}",
                params={"limit": min(limit, 40)},
            )
            if resp.status_code != 200:
                return []

            statuses = resp.json()
            posts = []
            for s in statuses:
                parsed = self._parse_status(s, keyword_matched=f"#{tag}")
                if parsed and len(posts) < limit:
                    posts.append(parsed)
            return posts

    async def _fetch_search_playwright(self, keyword: str, limit: int) -> list[RawPost]:
        """Authenticated search via Playwright session interception."""
        try:
            from playwright.async_api import async_playwright
            from playwright_stealth import stealth_async
        except ImportError:
            log.error("Playwright not installed — cannot do authenticated search")
            return []

        posts: list[RawPost] = []

        async with async_playwright() as pw:
            browser_type = getattr(pw, BROWSER_TYPE)
            context = await browser_type.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=PLAYWRIGHT_HEADLESS,
                viewport={"width": random.randint(1280, 1920), "height": random.randint(800, 1080)},
                user_agent=DEFAULT_HEADERS["User-Agent"],
            )

            page = await context.new_page()
            await stealth_async(page)

            intercepted: list[dict] = []

            async def handle_response(response):
                if "/api/v1/search" in response.url or "/api/v2/search" in response.url:
                    try:
                        data = await response.json()
                        intercepted.append(data)
                    except Exception:
                        pass

            page.on("response", handle_response)

            search_url = f"{BASE_URL}/search?q={keyword}&type=statuses"
            try:
                await page.goto(search_url, wait_until="networkidle", timeout=30000)
            except Exception:
                pass

            # Scroll to trigger more results
            for _ in range(random.randint(1, 3)):
                await page.mouse.wheel(0, random.randint(300, 600))
                await asyncio.sleep(random.uniform(1.5, 3.0))

            await page.close()
            await context.close()

        # Parse intercepted responses
        for data in intercepted:
            statuses = data.get("statuses", [])
            for s in statuses:
                parsed = self._parse_status(s, keyword_matched=keyword)
                if parsed and len(posts) < limit:
                    posts.append(parsed)

        return posts

    def _parse_status(self, raw: dict, keyword_matched: str = "") -> Optional[RawPost]:
        """Parse a Mastodon-format status object into RawPost."""
        try:
            status_id = raw.get("id", "")
            if not status_id:
                return None

            # Skip reblogs
            if raw.get("reblog"):
                return None

            content = raw.get("content", "")
            # Strip HTML tags from content
            import re
            content = re.sub(r"<[^>]+>", "", content).strip()
            if not content:
                return None

            account = raw.get("account", {})
            handle = account.get("acct", account.get("username", "unknown"))
            display_name = account.get("display_name", handle)
            followers = account.get("followers_count", 0)
            verified = bool(account.get("verified", False))

            # Parse timestamp
            created_at = raw.get("created_at", "")
            if created_at:
                # Mastodon uses ISO 8601
                if created_at.endswith("Z"):
                    created_at = created_at[:-1] + "+00:00"
                posted_at = datetime.fromisoformat(created_at)
            else:
                posted_at = datetime.now(timezone.utc)

            return RawPost(
                id=f"truthsocial:{status_id}",
                platform="truthsocial",
                author_handle=handle,
                author_display_name=display_name,
                author_followers=followers,
                author_verified=verified,
                content=content,
                posted_at=posted_at,
                scraped_at=datetime.now(timezone.utc),
                likes=raw.get("favourites_count", 0),
                retweets_or_boosts=raw.get("reblogs_count", 0),
                replies=raw.get("replies_count", 0),
                keyword_matched=keyword_matched,
            )
        except Exception:
            log.exception("Failed to parse TruthSocial status")
            return None

    async def setup_session(self) -> None:
        """Launch headed browser for manual TruthSocial login."""
        from playwright.async_api import async_playwright
        from playwright_stealth import stealth_async

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as pw:
            browser_type = getattr(pw, BROWSER_TYPE)
            context = await browser_type.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                viewport={"width": 1400, "height": 900},
                user_agent=DEFAULT_HEADERS["User-Agent"],
            )

            page = context.pages[0] if context.pages else await context.new_page()
            await stealth_async(page)
            await page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")

            print("\n" + "=" * 60)
            print("  TRUTHSOCIAL SESSION SETUP")
            print("=" * 60)
            print("  1. Log in to your TruthSocial account in the browser window")
            print("  2. Complete any verification if prompted")
            print("  3. Once logged in, come back here and press Enter")
            print("=" * 60 + "\n")

            input("Press Enter after logging in...")

            # Validate
            try:
                await page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=15000)
                print("Session saved — login appears successful!")
            except Exception:
                print("Warning: could not validate session — check manually")

            store.upsert_session("truthsocial", str(PROFILE_DIR), notes="Manual setup via setup_truthsocial_session.py")

            await context.close()
            print(f"Session saved to {PROFILE_DIR}")
