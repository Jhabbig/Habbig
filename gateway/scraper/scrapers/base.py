"""
Abstract base class for all platform scrapers.

Each scraper must implement:
  - fetch(keywords, limit) -> list[RawPost]
  - is_available() -> bool

And optionally override:
  - health_check() -> dict
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from scraper.storage.models import RawPost
from scraper.storage import db as store

log = logging.getLogger("scraper")


class BaseScraper(ABC):
    platform: str = ""  # override in subclass

    @abstractmethod
    async def fetch(self, keywords: list[str], limit: int = 100) -> list[RawPost]:
        """
        Scrape posts matching the given keywords.

        Args:
            keywords: Search terms to look for.
            limit: Maximum posts per keyword.

        Returns:
            List of RawPost objects (may include duplicates — caller deduplicates).
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """True if the scraper can run (session valid or no session needed)."""
        ...

    async def health_check(self) -> dict:
        """Return a status dict for the admin panel."""
        last_run = store.get_last_run(self.platform)
        session = store.get_session(self.platform)
        return {
            "platform": self.platform,
            "available": self.is_available(),
            "session_valid": session.valid if session else False,
            "last_successful_run": (
                last_run.completed_at.isoformat()
                if last_run and last_run.completed_at and not last_run.error
                else None
            ),
            "posts_collected_today": store.get_posts_today_count(self.platform),
            "error": last_run.error if last_run else None,
        }
