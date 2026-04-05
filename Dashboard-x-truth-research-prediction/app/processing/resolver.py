from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import select

from app.db import AsyncSession
from app.markets.polymarket import PolymarketClient
from app.models import Prediction, ResolvedMarket, SourcePredictionRecord

logger = logging.getLogger(__name__)


class MarketResolver:
    def __init__(self) -> None:
        self._poly = PolymarketClient()

    async def run(self, session: AsyncSession) -> dict:
        stats = {"markets_resolved": 0, "predictions_resolved": 0, "sources_recomputed": 0}
        closed_markets = await self._poly.fetch_closed_markets()
        affected_handles: set[str] = set()

        for m_data in closed_markets:
            slug = m_data.get("slug", m_data.get("conditionId", ""))
            if not slug:
                continue
            result = await session.exec(select(ResolvedMarket).where(ResolvedMarket.market_slug == slug))
            if result.first():
                continue
            outcome = self._poly.detect_resolution(m_data)
            if outcome is None:
                continue
            session.add(ResolvedMarket(market_slug=slug, outcome=outcome, resolved_at=datetime.now(timezone.utc)))
            stats["markets_resolved"] += 1

            pred_result = await session.exec(select(Prediction).where(Prediction.market_slug == slug, Prediction.resolved == False))  # noqa
            for pred in pred_result.all():
                pred.resolved = True
                pred.resolved_correct = self._check_correct(pred.predicted_outcome, outcome)
                session.add(pred)
                stats["predictions_resolved"] += 1
                spr_result = await session.exec(select(SourcePredictionRecord).where(SourcePredictionRecord.prediction_id == pred.id))
                spr = spr_result.first()
                if spr:
                    spr.resolved_correct = pred.resolved_correct
                    session.add(spr)
                    affected_handles.add(spr.handle)

        await session.commit()

        if affected_handles:
            from app.credibility.engine import CredibilityEngine
            eng = CredibilityEngine()
            for handle in affected_handles:
                try:
                    await eng.recompute(session, handle)
                    stats["sources_recomputed"] += 1
                except Exception as exc:
                    logger.error("Recompute %s failed: %s", handle, exc)
        return stats

    @staticmethod
    def _check_correct(predicted_outcome: str, actual_outcome: str) -> bool:
        return predicted_outcome.strip().lower() == actual_outcome.strip().lower()
