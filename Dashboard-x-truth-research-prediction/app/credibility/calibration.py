"""Calibration metrics — the standard rigor for forecast quality.

Hit-rate is an amateur metric. A source that says "55% chance" and is right
55% of the time is *better calibrated* than one that says "95%" and is right
60%, even though both have the same accuracy. Real prediction-market literature
uses Brier scores and reliability curves to capture this.

We compute:

  - **Brier score**: mean squared error between the predicted probability and
    the realised outcome (1 for YES win, 0 for NO win). Lower is better.
    A coin-flip "always 0.5" predictor scores 0.25. A perfect predictor scores 0.
    Only meaningful for predictions where the source supplied an explicit
    probability — we ignore directional ("will win") predictions for Brier.

  - **Reliability curve**: predictions binned by predicted probability. Each
    bin reports (predicted, observed, n). A perfectly calibrated source has
    `observed ≈ predicted` in every bin.

References:
  - Brier (1950), "Verification of forecasts expressed in terms of probability"
  - Murphy & Winkler (1987), "A general framework for forecast verification"
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass
class CalibrationStats:
    brier_score: Optional[float]
    n_scored: int  # number of probability-bearing predictions that contributed
    reliability_curve: list[tuple[float, float, int]]  # [(pred_mid, observed_freq, n)]


def _outcome_value(predicted_outcome: str, resolved_correct: bool) -> Optional[float]:
    """Translate a (predicted_outcome, resolved_correct) pair into an outcome
    suitable for scoring against ``predicted_probability``.

    Convention: ``predicted_probability`` is always P(YES). So:
      - predicted "Yes" and correct -> YES happened -> y=1
      - predicted "Yes" and wrong   -> NO  happened -> y=0
      - predicted "No"  and correct -> NO  happened -> y=0
      - predicted "No"  and wrong   -> YES happened -> y=1
    """
    pred = (predicted_outcome or "").strip().lower()
    if pred in ("yes", "true"):
        return 1.0 if resolved_correct else 0.0
    if pred in ("no", "false"):
        return 0.0 if resolved_correct else 1.0
    return None


def compute_calibration(predictions: Iterable, n_bins: int = 10) -> CalibrationStats:
    """Brier score + reliability curve from an iterable of resolved Prediction objects.

    Skips predictions without ``predicted_probability`` or ``resolved_correct``.
    Returns ``CalibrationStats(brier_score=None, n_scored=0, …)`` when there's
    no data to score.
    """
    scored: list[tuple[float, float]] = []  # (predicted_prob, realised_outcome)
    for p in predictions:
        prob = getattr(p, "predicted_probability", None)
        resolved = getattr(p, "resolved_correct", None)
        outcome_str = getattr(p, "predicted_outcome", "")
        if prob is None or resolved is None:
            continue
        y = _outcome_value(outcome_str, bool(resolved))
        if y is None:
            continue
        scored.append((max(0.0, min(1.0, float(prob))), y))

    if not scored:
        return CalibrationStats(brier_score=None, n_scored=0, reliability_curve=[])

    brier = sum((prob - y) ** 2 for prob, y in scored) / len(scored)

    # Reliability curve — bin by predicted prob.
    n_bins = max(2, min(20, n_bins))
    bins: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]
    for prob, y in scored:
        idx = min(n_bins - 1, int(prob * n_bins))
        bins[idx].append((prob, y))

    curve: list[tuple[float, float, int]] = []
    for i, bucket in enumerate(bins):
        if not bucket:
            continue
        pred_mid = (i + 0.5) / n_bins
        observed = sum(y for _, y in bucket) / len(bucket)
        curve.append((round(pred_mid, 3), round(observed, 4), len(bucket)))

    return CalibrationStats(
        brier_score=round(brier, 4),
        n_scored=len(scored),
        reliability_curve=curve,
    )
