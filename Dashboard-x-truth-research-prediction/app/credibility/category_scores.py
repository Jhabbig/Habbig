from __future__ import annotations

from collections import defaultdict

from sqlmodel import select

from app.config import yaml_config
from app.credibility.decay import decay_weighted_accuracy
from app.credibility.diversity import category_dominance_penalty
from app.db import AsyncSession
from app.models import SourcePredictionRecord


def smoothed_accuracy(correct: int, total: int, prior: float = 0.5, strength: int = 4) -> float:
    return (correct + prior * strength) / (total + strength)


async def compute_category_credibility(session: AsyncSession, handle: str, global_credibility: float) -> dict[str, float | None]:
    cfg = yaml_config.get("credibility", {})
    min_per_cat = cfg.get("min_predictions_per_category", 3)
    prior = cfg.get("bayesian_prior", 0.5)
    strength = cfg.get("bayesian_strength", 4)
    half_life = cfg.get("decay_half_life_days", 60.0)

    stmt = select(SourcePredictionRecord).where(SourcePredictionRecord.handle == handle, SourcePredictionRecord.counted == True, SourcePredictionRecord.resolved_correct.isnot(None))  # noqa
    result = await session.exec(stmt)
    records = result.all()
    if not records:
        return {}

    by_cat: dict[str, list] = defaultdict(list)
    for r in records:
        by_cat[r.category].append(r)

    scores: dict[str, float | None] = {}
    for cat, cat_records in by_cat.items():
        if len(cat_records) < min_per_cat:
            scores[cat] = None
            continue
        total = len(cat_records)
        correct = sum(1 for r in cat_records if r.resolved_correct)
        raw_acc = correct / total if total > 0 else 0.0
        dw_acc = decay_weighted_accuracy(cat_records, half_life)
        smoothed_dw = smoothed_accuracy(int(dw_acc * total), total, prior, strength)
        dom_penalty = category_dominance_penalty(records, cat)
        dominance_adjusted = max(0.0, 1.0 - dom_penalty)
        cat_score = dominance_adjusted * smoothed_dw * (0.6 * raw_acc + 0.4 * global_credibility)
        scores[cat] = round(min(1.0, max(0.0, cat_score)), 4)
    return scores
