"""Pipeline ingestion — feeds the scheduler's ranked predictions through Stage 3.

The API surface (app/engine/api.py) serves external callers; this module is the
internal queue path. After the pipeline ranks a prediction (Stage 1 credibility
recomputed, Stage 2 metrics extracted, market matched), it maps naturally onto
an EngineJob:

  credibility[]  ← the source's credibility score at prediction time
  metrics{}      ← the extracted predicted_probability + timing features
  context{}      ← the matched market (slug, implied probability, category)

Every fused prediction lands in fusion_audit, which is what gives the replay
harness real (prediction, outcome) pairs to grade and fit calibrators on.
Failures here never break the pipeline — fusion is best-effort per prediction.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config import settings
from app.engine.schemas import EngineJob
from app.models import Prediction, Source

logger = logging.getLogger(__name__)


def job_from_prediction(pred: Prediction, source: Optional[Source]) -> EngineJob:
    credibility = []
    if source is not None:
        cat_cred = source.category_credibility.get(pred.category) if source.category_credibility else None
        score = cat_cred if cat_cred is not None else source.global_credibility
        credibility.append({
            "post_id": pred.raw_post_id,
            "source": source.platform or "twitter",
            "score": max(0.0, min(1.0, float(score or 0.0))),
            "features": {
                "handle": source.handle,
                "accuracy_unlocked": source.accuracy_unlocked,
                "qualifying_predictions": source.qualifying_predictions,
            },
        })

    predicted: dict = {}
    if pred.predicted_probability is not None:
        predicted["predicted_probability"] = pred.predicted_probability
    extracted: dict = {}
    if pred.hours_remaining_at_prediction is not None:
        extracted["hours_remaining"] = pred.hours_remaining_at_prediction

    context: dict = {"category": pred.category}
    if pred.market_slug:
        context["market_slug"] = pred.market_slug
    if pred.market_implied_probability is not None:
        context["market_implied_probability"] = pred.market_implied_probability

    return EngineJob(
        job_id=f"pipeline-{pred.id}",
        user_id="pipeline",
        job_class="pipeline",
        credibility=credibility,
        metrics={
            "predicted": predicted,
            "extracted": extracted,
            "model_version": settings.get("LLM_EXTRACTOR_MODEL", "regex-v1"),
        },
        context=context,
    )


async def fuse_ranked_predictions(pairs: list[tuple[Prediction, Optional[Source]]]) -> int:
    """Run each (prediction, source) pair through the engine. Best-effort."""
    from app.engine.service import get_engine

    engine = get_engine()
    fused = 0
    for pred, source in pairs:
        try:
            await engine.predict(job_from_prediction(pred, source))
            fused += 1
        except Exception as exc:
            logger.warning("Stage-3 fusion failed for prediction %s: %s", pred.id, exc)
    return fused
