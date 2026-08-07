from __future__ import annotations

from typing import Optional

from app.config import yaml_config
from app.models import Prediction, Source


def shrink_toward_base(accuracy: float, n: int, base_rate: float, k: int = 10) -> float:
    # Pseudo-count prior: (hits + k*base)/(n + k). Low-n sources get pulled toward
    # the category base rate so a 3/3 record doesn't read as a 100% hit rate.
    n = max(0, n)
    return (accuracy * n + k * base_rate) / (n + k)


def compute_ev_score(predicted_prob: float, market_implied_prob: float) -> Optional[float]:
    # Plain probability gap. The old 1/market_prob multiplier amplified longshots
    # and made the ranking structurally negative-EV (2026-08 audit).
    if market_implied_prob <= 0 or market_implied_prob >= 1:
        return None
    return predicted_prob - market_implied_prob


def _category_base_rate(category: str) -> float:
    cfg = yaml_config.get("scoring", {})
    rates = cfg.get("category_base_rates", {}) or {}
    rate = rates.get(category)
    return rate if rate is not None else cfg.get("default_base_rate", 0.5)


def compute_risk_flags(prediction: Prediction, source: Source | None) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    cfg = yaml_config.get("scoring", {}).get("risk_thresholds", {})
    cred_cfg = yaml_config.get("credibility", {})

    if source is None:
        reasons.append("Source not yet rated (insufficient history)")
    else:
        if not source.accuracy_unlocked:
            reasons.append("Source not yet rated (insufficient history)")
        if source.global_credibility < cfg.get("min_global_credibility", 0.4):
            reasons.append("Low global credibility")
        cat_cred = source.category_credibility.get(prediction.category)
        if cat_cred is not None and cat_cred < cfg.get("min_category_credibility", 0.35):
            reasons.append("Weak in this category")
        if source.qualifying_predictions < cred_cfg.get("min_qualifying_predictions", 10):
            reasons.append("Insufficient prediction history")
        if len(source.categories_predicted_in) < cred_cfg.get("min_categories_for_unlock", 3):
            reasons.append("Too specialised — potential gaming")
        if source.trusted is False:
            reasons.append("Manually flagged as untrusted")

    if prediction.market_implied_probability is not None:
        lo, hi = cfg.get("extreme_market_bounds", [0.05, 0.95])
        if prediction.market_implied_probability < lo or prediction.market_implied_probability > hi:
            reasons.append("Extreme market — low signal")
    if prediction.ev_score is not None and prediction.ev_score < 0:
        reasons.append("Negative expected value")
    if prediction.hours_remaining_at_prediction is not None and prediction.hours_remaining_at_prediction < 12:
        reasons.append("Prediction too close to market close")

    return (len(reasons) > 0, reasons)


def rank_prediction(prediction: Prediction, source: Source | None) -> Prediction:
    if prediction.market_implied_probability is not None and prediction.market_slug is not None:
        cfg = yaml_config.get("scoring", {})
        k = cfg.get("ev_shrinkage_pseudo_count", 10)
        base = _category_base_rate(prediction.category)
        if prediction.predicted_probability is not None:
            pred_prob = prediction.predicted_probability
        else:
            cat_cred = source.category_credibility.get(prediction.category) if source else None
            if cat_cred is not None:
                pred_prob = shrink_toward_base(cat_cred, source.qualifying_predictions, base, k)
            elif source:
                pred_prob = shrink_toward_base(source.global_credibility, source.qualifying_predictions, base, k)
            else:
                pred_prob = base
        prediction.ev_score = compute_ev_score(pred_prob, prediction.market_implied_probability)

    if source:
        prediction.global_credibility_at_time = source.global_credibility
        prediction.category_credibility_at_time = source.category_credibility.get(prediction.category)

    risk_flag, risk_reasons = compute_risk_flags(prediction, source)
    prediction.risk_flag = risk_flag
    prediction.risk_reasons = risk_reasons
    return prediction
