from __future__ import annotations

from app.config import yaml_config

_spread_map = {int(k): float(v) for k, v in yaml_config.get("credibility", {}).get("spread_penalty", {1: 0.30, 2: 0.60, 3: 0.85, 4: 1.00}).items()}


def category_spread_penalty(categories_predicted_in: list[str]) -> float:
    n = len(set(categories_predicted_in))
    if n <= 0:
        return 0.0
    if n >= 4:
        return _spread_map.get(4, 1.00)
    return _spread_map.get(n, 1.00)


def category_dominance_penalty(records: list, category: str) -> float:
    cfg = yaml_config.get("credibility", {})
    threshold = cfg.get("category_dominance_threshold", 0.60)
    penalty = cfg.get("category_dominance_penalty", 0.15)
    if not records:
        return 0.0
    total = len(records)
    cat_count = sum(1 for r in records if getattr(r, "category", "") == category)
    if total > 0 and (cat_count / total) > threshold:
        return penalty
    return 0.0
