from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from sqlmodel import func, select

from app.config import yaml_config
from app.credibility.calibration import compute_calibration
from app.credibility.category_scores import compute_category_credibility, smoothed_accuracy
from app.credibility.decay import decay_weighted_accuracy
from app.credibility.diversity import category_spread_penalty
from app.db import AsyncSession
from app.models import CredibilitySnapshot, Prediction, RawPost, Source, SourcePredictionRecord

logger = logging.getLogger(__name__)


class CredibilityEngine:
    def __init__(self) -> None:
        c = yaml_config.get("credibility", {})
        w = c.get("weights", {})
        self._w_accuracy = w.get("accuracy", 0.45)
        self._w_engagement = w.get("engagement", 0.18)
        self._w_verified = w.get("verified", 0.12)
        self._w_volume = w.get("volume", 0.12)
        self._w_manual = w.get("manual", 0.13)
        self._trust_delta = c.get("manual_trust_delta", {"trusted": 0.20, "untrusted": -0.40})
        self._min_qualifying = c.get("min_qualifying_predictions", 10)
        self._min_cats = c.get("min_categories_for_unlock", 3)
        self._prior = c.get("bayesian_prior", 0.5)
        self._strength = c.get("bayesian_strength", 4)
        self._half_life = c.get("decay_half_life_days", 60.0)

    async def recompute(self, session: AsyncSession, handle: str) -> Source:
        stmt = select(Source).where(Source.handle == handle)
        result = await session.exec(stmt)
        source = result.first()
        if not source:
            source = Source(handle=handle)
            session.add(source)

        rec_result = await session.exec(select(SourcePredictionRecord).where(SourcePredictionRecord.handle == handle, SourcePredictionRecord.counted == True))  # noqa
        all_records = rec_result.all()
        all_result = await session.exec(select(SourcePredictionRecord).where(SourcePredictionRecord.handle == handle))
        all_predictions = all_result.all()

        source.total_predictions = len(all_predictions)
        source.qualifying_predictions = len(all_records)
        categories = list({r.category for r in all_records if r.category})
        source.categories_predicted_in = categories
        resolved = [r for r in all_records if r.resolved_correct is not None]
        correct = sum(1 for r in resolved if r.resolved_correct)
        source.correct_qualifying = correct
        source.accuracy_unlocked = source.qualifying_predictions >= self._min_qualifying and len(categories) >= self._min_cats

        if source.accuracy_unlocked and resolved:
            source.accuracy_global = correct / len(resolved) if resolved else 0.0
            dw_acc = decay_weighted_accuracy(resolved, self._half_life)
            source.decay_weighted_accuracy = dw_acc
            smoothed_dw = smoothed_accuracy(int(dw_acc * len(resolved)), len(resolved), self._prior, self._strength)
            spread = category_spread_penalty(categories)
            accuracy_component = self._w_accuracy * spread * smoothed_dw
        else:
            source.accuracy_global = None
            source.decay_weighted_accuracy = None
            accuracy_component = 0.0

        engagement_component = self._w_engagement * min(source.engagement_ratio, 1.0)
        verified_component = self._w_verified * (1.0 if source.verified else 0.0)
        max_result = await session.exec(select(func.max(Source.follower_count)))
        max_followers = max_result.first() or 1
        volume_component = self._w_volume * (math.log10(source.follower_count + 1) / math.log10(max_followers + 1)) if max_followers > 0 and source.follower_count > 0 else 0.0

        if source.trusted is True:
            manual_component = self._w_manual * self._trust_delta.get("trusted", 0.20)
        elif source.trusted is False:
            manual_component = self._w_manual * self._trust_delta.get("untrusted", -0.40)
        else:
            manual_component = 0.0

        source.global_credibility = round(max(0.0, min(1.0, accuracy_component + engagement_component + verified_component + volume_component + manual_component)), 4)
        cat_cred = await compute_category_credibility(session, handle, source.global_credibility)
        source.category_credibility = cat_cred

        # Calibration metrics: Brier score over the source's probability-bearing
        # resolved predictions. Joins back to the prediction rows via raw_post_id
        # (SPR doesn't carry predicted_outcome / predicted_probability itself).
        try:
            stmt = (
                select(Prediction)
                .join(RawPost, Prediction.raw_post_id == RawPost.id)
                .where(
                    RawPost.author_handle == handle,
                    Prediction.predicted_probability.isnot(None),
                    Prediction.resolved == True,  # noqa: E712
                    Prediction.resolved_correct.isnot(None),
                )
            )
            preds = (await session.exec(stmt)).all()
            calib = compute_calibration(preds)
            source.brier_score = calib.brier_score
            source.brier_n = calib.n_scored
        except Exception as exc:
            logger.warning("Calibration compute failed for %s: %s", handle, exc)

        source.last_seen = datetime.now(timezone.utc)
        session.add(source)

        snapshot = CredibilitySnapshot(handle=handle, snapshotted_at=datetime.now(timezone.utc), global_credibility=source.global_credibility, accuracy_unlocked=source.accuracy_unlocked)
        snapshot.category_credibility = cat_cred
        session.add(snapshot)
        await session.commit()
        return source

    async def recompute_all(self, session: AsyncSession) -> int:
        result = await session.exec(select(Source.handle))
        handles = result.all()
        count = 0
        for handle in handles:
            try:
                await self.recompute(session, handle)
                count += 1
            except Exception as exc:
                logger.error("Recompute %s failed: %s", handle, exc)
        return count
