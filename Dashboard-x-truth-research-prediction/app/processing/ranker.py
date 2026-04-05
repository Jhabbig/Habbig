from __future__ import annotations

from typing import Optional

from app.config import yaml_config
from app.models import Prediction, Source


def compute_ev_score(predicted_prob: float, market_implied_prob: float) -> Optional[float]:
    if market_implied_prob <= 0 or market_implied_prob >= 1:
        return None
    return (predicted_prob - market_implied_prob) * (1.0 / market_implied_prob)


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
        if prediction.predicted_probability is not None:
            pred_prob = prediction.predicted_probability
        elif source and prediction.category in source.category_credibility:
            pred_prob = source.category_credibility[prediction.category]
        else:
            pred_prob = source.global_credibility if source else 0.5
        prediction.ev_score = compute_ev_score(pred_prob, prediction.market_implied_probability)

    if source:
        prediction.global_credibility_at_time = source.global_credibility
        prediction.category_credibility_at_time = source.category_credibility.get(prediction.category)

    risk_flag, risk_reasons = compute_risk_flags(prediction, source)
    prediction.risk_flag = risk_flag
    prediction.risk_reasons = risk_reasons
    return prediction
