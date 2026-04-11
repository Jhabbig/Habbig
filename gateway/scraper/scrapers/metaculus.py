"""Metaculus API scraper (F11).

Fetches prediction questions and community forecasts from the Metaculus
public API. No auth required — all data is publicly accessible.

Source handles: "metaculus:{question_id}"
Categories: mapped from Metaculus tags to our standard categories.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from scraper.scrapers.base import BaseScraper
from scraper.storage.models import RawPost

log = logging.getLogger("scraper.metaculus")

API_BASE = "https://www.metaculus.com/api2"

# Map Metaculus categories to our standard categories.
_CATEGORY_MAP = {
    "us-politics": "politics",
    "elections": "politics",
    "geopolitics": "geopolitics",
    "ai": "crypto",  # close enough for now
    "economics": "economics",
    "sports": "sports",
    "climate": "weather",
    "science": "other",
}


class MetaculusScraper(BaseScraper):
    """Scraper for the Metaculus public API."""

    platform = "metaculus"

    async def fetch(self, keywords: list[str], limit: int = 100) -> list[RawPost]:
        posts: list[RawPost] = []
        seen_ids: set[int] = set()

        async with httpx.AsyncClient(timeout=15) as client:
            for kw in keywords:
                try:
                    resp = await client.get(
                        f"{API_BASE}/questions/",
                        params={
                            "search": kw,
                            "limit": min(limit, 50),
                            "status": "open",
                            "type": "forecast",
                            "order_by": "-activity",
                        },
                    )
                    if resp.status_code != 200:
                        log.warning("Metaculus API returned %d for '%s'", resp.status_code, kw)
                        continue

                    data = resp.json()
                    results = data.get("results", [])

                    for q in results:
                        qid = q.get("id")
                        if not qid or qid in seen_ids:
                            continue
                        seen_ids.add(qid)

                        # Extract community prediction if available
                        community_pred = q.get("community_prediction", {})
                        prob = None
                        if isinstance(community_pred, dict):
                            prob = community_pred.get("full", {}).get("q2")
                        elif isinstance(community_pred, (int, float)):
                            prob = float(community_pred)

                        title = q.get("title", "")
                        description = (q.get("description") or "")[:500]
                        content = f"{title}\n{description}".strip()
                        if prob is not None:
                            content += f"\n[Metaculus community: {prob:.0%}]"

                        # Parse dates
                        created_str = q.get("created_time") or q.get("publish_time") or ""
                        try:
                            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        except (ValueError, AttributeError):
                            created = datetime.now(timezone.utc)

                        post = RawPost(
                            id=f"metaculus:{qid}",
                            platform="metaculus",
                            author_handle=f"metaculus:community",
                            author_display_name="Metaculus Community",
                            author_followers=0,
                            author_verified=True,
                            content=content,
                            posted_at=created,
                            scraped_at=datetime.now(timezone.utc),
                            likes=q.get("votes", {}).get("up", 0) if isinstance(q.get("votes"), dict) else 0,
                            retweets_or_boosts=0,
                            replies=q.get("comment_count", 0) or 0,
                            keyword_matched=kw,
                        )
                        posts.append(post)

                except httpx.RequestError as e:
                    log.warning("Metaculus fetch error for '%s': %s", kw, e)
                except Exception as e:
                    log.exception("Metaculus scraper error for '%s': %s", kw, e)

        log.info("Metaculus: fetched %d questions for %d keywords", len(posts), len(keywords))
        return posts[:limit]

    def is_available(self) -> bool:
        return True  # Public API, no auth needed

    async def health_check(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{API_BASE}/questions/", params={"limit": 1})
                return {"available": resp.status_code == 200, "status_code": resp.status_code}
        except Exception as e:
            return {"available": False, "error": str(e)}
