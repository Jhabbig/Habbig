from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import yaml_config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    def __init__(self) -> None:
        self._base_url = BASE_URL
        self._category_keywords = yaml_config.get("scraping", {}).get("keywords", {}).get("category_keywords", {})

    async def fetch_active_markets(self, limit: int = 200, max_pages: int = 5) -> list[dict]:
        all_markets: list[dict] = []
        cursor = None
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(max_pages):
                try:
                    params: dict = {"limit": min(limit, 200)}
                    if cursor:
                        params["cursor"] = cursor
                    resp = await client.get(f"{self._base_url}/markets", params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    markets = data.get("markets", [])
                    if not markets:
                        break
                    all_markets.extend(markets)
                    cursor = data.get("cursor")
                    if not cursor:
                        break
                except Exception as exc:
                    logger.error("Kalshi fetch failed: %s", exc)
                    break
        logger.info("Kalshi: fetched %d active markets", len(all_markets))
        return all_markets

    async def fetch_settled_markets(self, limit: int = 100) -> list[dict]:
        markets: list[dict] = []
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(
                    f"{self._base_url}/markets",
                    params={"limit": min(limit, 200), "status": "finalized"},
                )
                resp.raise_for_status()
                data = resp.json()
                markets = data.get("markets", [])
            except Exception as exc:
                logger.error("Kalshi settled fetch failed: %s", exc)
        return markets

    def categorize_market(self, title: str, subtitle: str = "") -> str:
        text = f"{title} {subtitle}".lower()
        best_cat, best_count = "other", 0
        for category, keywords in self._category_keywords.items():
            count = 0
            for kw in keywords:
                if len(kw) <= 4:
                    if re.search(r'\b' + re.escape(kw.lower()) + r'\b', text):
                        count += 1
                else:
                    if kw.lower() in text:
                        count += 1
            if count > best_count:
                best_count = count
                best_cat = category
        return best_cat

    def detect_resolution(self, market_data: dict) -> Optional[str]:
        result = market_data.get("result", "")
        if result in ("yes", "no"):
            return result.capitalize()
        return None

    async def sync_markets(self, session) -> tuple[int, int]:
        from sqlmodel import select
        from app.models import MarketSnapshot

        raw_markets = await self.fetch_active_markets()
        new_count, updated_count = 0, 0

        for m in raw_markets:
            ticker = m.get("ticker", "")
            if not ticker:
                continue

            title = m.get("title", m.get("yes_sub_title", ""))
            category = self.categorize_market(title, m.get("no_sub_title", ""))

            # Price: Kalshi returns dollars as strings like "0.6500"
            yes_price = float(m.get("yes_bid_dollars") or m.get("last_price_dollars") or 0)

            volume = float(m.get("volume_fp") or m.get("notional_value_dollars") or 0)

            close_str = m.get("close_time") or m.get("latest_expiration_time")
            close_time = None
            if close_str:
                try:
                    close_time = datetime.fromisoformat(str(close_str).replace("Z", "+00:00"))
                except ValueError:
                    pass

            # Check existing
            stmt = select(MarketSnapshot).where(
                MarketSnapshot.market_slug == ticker,
                MarketSnapshot.platform == "kalshi",
            ).order_by(MarketSnapshot.snapshotted_at.desc())
            result = await session.exec(stmt)
            existing = result.first()

            if existing:
                existing.yes_price = yes_price
                existing.volume_usd = volume
                existing.close_time = close_time
                existing.snapshotted_at = datetime.now(timezone.utc)
                session.add(existing)
                updated_count += 1
            else:
                session.add(MarketSnapshot(
                    market_slug=ticker,
                    market_question=title,
                    category=category,
                    yes_price=yes_price,
                    volume_usd=volume,
                    close_time=close_time,
                    platform="kalshi",
                    snapshotted_at=datetime.now(timezone.utc),
                ))
                new_count += 1

        await session.commit()
        logger.info("Kalshi sync: %d new, %d updated", new_count, updated_count)
        return new_count, updated_count
