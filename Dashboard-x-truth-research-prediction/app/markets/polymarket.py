from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import yaml_config

logger = logging.getLogger(__name__)
BASE_URL = "https://gamma-api.polymarket.com"


class PolymarketClient:
    def __init__(self) -> None:
        self._base_url = BASE_URL
        self._category_keywords = yaml_config.get("scraping", {}).get("keywords", {}).get("category_keywords", {})

    async def fetch_active_markets(self, limit: int = 100, max_pages: int = 10) -> list[dict]:
        all_markets = []
        async with httpx.AsyncClient(timeout=30) as client:
            for page in range(max_pages):
                try:
                    resp = await client.get(f"{self._base_url}/markets", params={"active": "true", "closed": "false", "limit": limit, "offset": page * limit})
                    resp.raise_for_status()
                    batch = resp.json()
                    if not batch:
                        break
                    all_markets.extend(batch)
                except Exception as exc:
                    logger.error("Polymarket page %d failed: %s", page, exc)
                    break
        return all_markets

    async def fetch_closed_markets(self, limit: int = 50) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(f"{self._base_url}/markets", params={"closed": "true", "limit": limit, "offset": 0})
                resp.raise_for_status()
                return resp.json() or []
            except Exception as exc:
                logger.error("Polymarket closed fetch failed: %s", exc)
                return []

    def categorize_market(self, question: str, event_title: str = "") -> str:
        text = f"{question} {event_title}".lower()
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

    @staticmethod
    def parse_prices(market_data: dict) -> list[float]:
        raw = market_data.get("outcomePrices", [])
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return []
        try:
            return [float(p) for p in raw]
        except (ValueError, TypeError):
            return []

    @staticmethod
    def parse_outcomes(market_data: dict) -> list[str]:
        raw = market_data.get("outcomes", [])
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return []
        return [str(o) for o in raw] if raw else []

    def detect_resolution(self, market_data: dict) -> Optional[str]:
        if not market_data.get("closed", False):
            return None
        prices = self.parse_prices(market_data)
        outcomes = self.parse_outcomes(market_data)
        if not prices or not outcomes or len(prices) != len(outcomes):
            return None
        max_price = max(prices)
        if max_price > 0.99:
            return outcomes[prices.index(max_price)]
        return None

    async def sync_markets(self, session) -> tuple[int, int]:
        from sqlmodel import select
        from app.models import MarketSnapshot

        raw_markets = await self.fetch_active_markets()
        new_count, updated_count = 0, 0
        for m in raw_markets:
            slug = m.get("slug", m.get("conditionId", ""))
            if not slug:
                continue
            question = m.get("question", "")
            category = self.categorize_market(question, m.get("groupItemTitle", ""))
            prices = self.parse_prices(m)
            yes_price = prices[0] if prices else 0.0
            volume = float(m.get("volumeNum", m.get("volume", 0)) or 0)
            end_str = m.get("endDate") or m.get("end_date")
            close_time = None
            if end_str:
                try:
                    close_time = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
                except ValueError:
                    pass
            stmt = select(MarketSnapshot).where(MarketSnapshot.market_slug == slug).order_by(MarketSnapshot.snapshotted_at.desc())
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
                session.add(MarketSnapshot(market_slug=slug, market_question=question, category=category, yes_price=yes_price, volume_usd=volume, close_time=close_time, snapshotted_at=datetime.now(timezone.utc)))
                new_count += 1
        await session.commit()
        logger.info("Polymarket sync: %d new, %d updated", new_count, updated_count)
        return new_count, updated_count
