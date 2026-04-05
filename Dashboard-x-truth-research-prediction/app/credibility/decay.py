from __future__ import annotations

import math
from datetime import datetime, timezone

from app.config import yaml_config


def decay_weight(predicted_at: datetime, half_life_days: float | None = None) -> float:
    if half_life_days is None:
        half_life_days = yaml_config.get("credibility", {}).get("decay_half_life_days", 60.0)
    now = datetime.now(timezone.utc)
    pred_utc = predicted_at.replace(tzinfo=timezone.utc) if predicted_at.tzinfo is None else predicted_at
    days_elapsed = max(0.0, (now - pred_utc).total_seconds() / 86400.0)
    return math.pow(0.5, days_elapsed / half_life_days)


def decay_weighted_accuracy(records: list, half_life_days: float | None = None) -> float:
    if half_life_days is None:
        half_life_days = yaml_config.get("credibility", {}).get("decay_half_life_days", 60.0)
    resolved = [r for r in records if r.resolved_correct is not None]
    if not resolved:
        return 0.0
    correct_w = sum(decay_weight(r.predicted_at, half_life_days) for r in resolved if r.resolved_correct)
    total_w = sum(decay_weight(r.predicted_at, half_life_days) for r in resolved)
    return correct_w / total_w if total_w > 0 else 0.0
