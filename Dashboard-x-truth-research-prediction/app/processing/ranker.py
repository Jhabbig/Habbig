from __future__ import annotations

from typing import Optional

from app.config import yaml_config
from app.models import Prediction, Source


def compute_ev_score(predicted_prob: float, market_implied_prob: float) -> Optional[float]:
    """Expected return per $1 staked on the YES side at the market's implied YES price.

    Algebraically equivalent to ``(predicted_prob - market_implied_prob) / market_implied_prob``.
    """
    if market_implied_prob <= 0 or market_implied_prob >= 1:
        return None
    return predicted_prob / market_implied_prob - 1.0


def best_side_ev(predicted_prob: float, market_implied_prob: float) -> tuple[Optional[float], str]:
    """Pick the better of (buy YES) vs (buy NO) and return (ev_per_dollar, side).

    The previous implementation only scored the YES side, so a prediction of
    ``predicted=0.20`` against a market priced at ``0.80`` returned a large
    negative number — hiding the symmetric, profitable BUY-NO trade. This
    returns the higher-EV side, which is what users actually want surfaced.
    """
    if market_implied_prob <= 0 or market_implied_prob >= 1:
        return None, "YES"
    yes_ev = predicted_prob / market_implied_prob - 1.0
    no_ev = (1.0 - predicted_prob) / (1.0 - market_implied_prob) - 1.0
    if yes_ev >= no_ev:
        return yes_ev, "YES"
    return no_ev, "NO"


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


def _pred_prob_yes(prediction: Prediction, source: Source | None) -> float:
    """Translate the source's stated outcome + skill into a P(YES) belief.

    - If the extractor produced an explicit probability (e.g. "75% chance"),
      treat that as P(YES).
    - Otherwise we fall back to the source's category credibility (or global
      credibility), and flip it when the source predicted "No": a 0.65-credible
      source saying NO implies P(YES) = 0.35.
    """
    if prediction.predicted_probability is not None:
        return prediction.predicted_probability
    if source is None:
        return 0.5
    cat_cred = source.category_credibility.get(prediction.category) if source.category_credibility else None
    cred = cat_cred if cat_cred is not None else source.global_credibility
    if (prediction.predicted_outcome or "").strip().lower() in ("no", "false"):
        return max(0.0, min(1.0, 1.0 - cred))
    return max(0.0, min(1.0, cred))


def rank_prediction(prediction: Prediction, source: Source | None) -> Prediction:
    if prediction.market_implied_probability is not None and prediction.market_slug is not None:
        pred_prob = _pred_prob_yes(prediction, source)
        ev, side = best_side_ev(pred_prob, prediction.market_implied_probability)
        prediction.ev_score = ev
        prediction.bet_side = side

    if source:
        prediction.global_credibility_at_time = source.global_credibility
        prediction.category_credibility_at_time = source.category_credibility.get(prediction.category)

    risk_flag, risk_reasons = compute_risk_flags(prediction, source)
    prediction.risk_flag = risk_flag
    prediction.risk_reasons = risk_reasons
    return prediction
