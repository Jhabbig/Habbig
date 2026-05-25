from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import select

from app.db import AsyncSession
from app.markets.kalshi import KalshiClient
from app.markets.polymarket import PolymarketClient
from app.models import Prediction, ResolvedMarket, SourcePredictionRecord

logger = logging.getLogger(__name__)


_YES_TOKENS = frozenset(["yes", "true", "y", "1"])
_NO_TOKENS = frozenset(["no", "false", "n", "0"])


class MarketResolver:
    def __init__(self) -> None:
        self._poly = PolymarketClient()
        self._kalshi = KalshiClient()

    async def run(self, session: AsyncSession) -> dict:
        stats = {"markets_resolved": 0, "predictions_resolved": 0, "sources_recomputed": 0}
        affected_handles: set[str] = set()

        # Resolve from both venues. Each (slug, outcome) is a closed market.
        for venue in (self._poly, self._kalshi):
            try:
                if isinstance(venue, KalshiClient):
                    closed_markets = await venue.fetch_settled_markets()
                    slug_key = lambda m: m.get("ticker", "")  # noqa: E731
                else:
                    closed_markets = await venue.fetch_closed_markets()
                    slug_key = lambda m: m.get("slug", m.get("conditionId", ""))  # noqa: E731
            except Exception as exc:
                logger.error("Resolver fetch failed: %s", exc)
                continue

            for m_data in closed_markets:
                slug = slug_key(m_data)
                if not slug:
                    continue
                already = await session.exec(select(ResolvedMarket).where(ResolvedMarket.market_slug == slug))
                if already.first():
                    continue
                outcome = venue.detect_resolution(m_data)
                if outcome is None:
                    continue
                session.add(ResolvedMarket(market_slug=slug, outcome=outcome, resolved_at=datetime.now(timezone.utc)))
                stats["markets_resolved"] += 1

                pred_result = await session.exec(select(Prediction).where(Prediction.market_slug == slug, Prediction.resolved == False))  # noqa: E712
                for pred in pred_result.all():
                    pred.resolved = True
                    pred.resolved_correct = self._bet_won(pred, outcome)
                    session.add(pred)
                    stats["predictions_resolved"] += 1
                    spr_result = await session.exec(select(SourcePredictionRecord).where(SourcePredictionRecord.prediction_id == pred.id))
                    spr = spr_result.first()
                    if spr:
                        spr.resolved_correct = pred.resolved_correct
                        session.add(spr)
                        affected_handles.add(spr.handle)

                # Settle every open paper trade on this market in one shot.
                # Pass both the binary tri-state (won_yes) AND the raw outcome
                # string so the settlement logic can handle multi-outcome
                # markets (where won_yes is None but the named outcome is
                # required to settle each candidate's trade correctly).
                from app.processing.paper_trade import settle_trades_for_market
                won_yes = self._won_yes(outcome)
                stats["paper_trades_settled"] = stats.get("paper_trades_settled", 0) + await settle_trades_for_market(session, slug, won_yes, outcome=outcome)

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
    def _bet_won(prediction: Prediction, outcome: str) -> bool:
        """Did the prediction's bet (YES or NO side) match the resolved outcome?

        Handles three cases:
        1. Binary outcome ("Yes"/"No"/"True"/"False") — compare to ``bet_side``.
        2. Named outcome (e.g. "Trump" on a multi-outcome market) — fall back to
           text equality with ``predicted_outcome``.
        3. Anything else — fall back to text equality (preserves legacy behavior).
        """
        o = (outcome or "").strip().lower()
        side = (prediction.bet_side or "YES").upper()
        if o in _YES_TOKENS:
            return side == "YES"
        if o in _NO_TOKENS:
            return side == "NO"
        pred = (prediction.predicted_outcome or "").strip().lower()
        return bool(pred) and pred == o

    @staticmethod
    def _check_correct(predicted_outcome: str, actual_outcome: str) -> bool:
        """Legacy string-match helper — retained for backward compatibility with tests."""
        return (predicted_outcome or "").strip().lower() == (actual_outcome or "").strip().lower()

    @staticmethod
    def _won_yes(outcome: str) -> bool | None:
        """Translate a market outcome into a tri-state for paper-trade settlement.

        Returns True if YES won, False if NO won, None for non-binary outcomes
        (e.g. multi-candidate markets) where we can't simply settle YES/NO bets.
        """
        o = (outcome or "").strip().lower()
        if o in _YES_TOKENS:
            return True
        if o in _NO_TOKENS:
            return False
        return None
