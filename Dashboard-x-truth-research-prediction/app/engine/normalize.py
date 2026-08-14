"""Signal normalization — everything the fusion sees is on a comparable [0,1] scale.

Four signal families feed fusion v0 (weights in config.yaml → engine.fusion.weights):
  credibility           — mean of Component 1 per-post scores (already 0-1)
  predicted_probability — Component 2's LLM-predicted P(YES) (already 0-1)
  market_implied        — live market YES price from context (already 0-1)
  extracted_features    — mean of extracted numeric metrics, each min-max
                          normalized via engine.fusion.metric_ranges

A missing family is simply absent from the returned list (the fusion
renormalizes weights over what's present); the service layer turns absences
into degraded flags + confidence penalties.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from app.engine.fusion import SignalReading
from app.engine.schemas import EngineJob


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _as_float(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_credibility(job: EngineJob) -> Optional[float]:
    scores = [clamp01(item.score) for item in job.credibility]
    if not scores:
        return None
    return sum(scores) / len(scores)


def normalize_predicted_probability(job: EngineJob, cfg: dict) -> Optional[float]:
    key = cfg["fusion"].get("predicted_probability_key", "predicted_probability")
    value = _as_float(job.metrics.predicted.get(key))
    if value is None:
        return None
    return clamp01(value)


def normalize_market_implied(job: EngineJob) -> Optional[float]:
    value = _as_float(job.context.get("market_implied_probability"))
    if value is None or value <= 0.0 or value >= 1.0:
        # extreme/degenerate market prices carry no usable signal
        return None
    return value


def normalize_extracted_features(job: EngineJob, cfg: dict) -> Optional[float]:
    ranges: dict = cfg["fusion"].get("metric_ranges", {}) or {}
    normalized: List[float] = []
    for name, raw in job.metrics.extracted.items():
        value = _as_float(raw)
        if value is None:
            continue
        span = ranges.get(name)
        if span and len(span) == 2 and span[1] > span[0]:
            normalized.append(clamp01((value - span[0]) / (span[1] - span[0])))
        elif 0.0 <= value <= 1.0:
            normalized.append(value)  # already a proportion — trust it
        # otherwise: unbounded metric with no configured range — skip rather than guess
    if not normalized:
        return None
    return sum(normalized) / len(normalized)


def build_signals(job: EngineJob, cfg: dict) -> Tuple[List[SignalReading], List[str]]:
    """Normalize the job into weighted signal readings.

    Returns (signals_present, missing_component_reasons). Reasons use the
    degraded-flag vocabulary: credibility_unavailable / metrics_unavailable.
    """
    weights: dict = cfg["fusion"]["weights"]
    signals: List[SignalReading] = []
    reasons: List[str] = []

    cred = normalize_credibility(job)
    if cred is not None:
        signals.append(SignalReading("credibility", cred, weights.get("credibility", 0.0)))
    else:
        reasons.append("credibility_unavailable")

    pred = normalize_predicted_probability(job, cfg)
    if pred is not None:
        signals.append(SignalReading("predicted_probability", pred, weights.get("predicted_probability", 0.0)))

    extracted = normalize_extracted_features(job, cfg)
    if extracted is not None:
        signals.append(SignalReading("extracted_features", extracted, weights.get("extracted_features", 0.0)))

    if pred is None and extracted is None:
        reasons.append("metrics_unavailable")

    market = normalize_market_implied(job)
    if market is not None:
        signals.append(SignalReading("market_implied", market, weights.get("market_implied", 0.0)))

    signals = [s for s in signals if s.weight > 0.0]
    return signals, reasons
