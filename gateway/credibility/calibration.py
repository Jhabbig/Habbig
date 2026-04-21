"""Brier-score calibration for sources.

Calibration asks: when a source said "70% chance", did roughly 70% of
those predictions come true? It's orthogonal to accuracy — a source can
be 65% accurate but badly miscalibrated (claiming 95% confidence on
every call), and calibration is what we weight when picking best-bets.

Input is a list of resolved prediction records with:
  predicted_probability_stated  float in [0, 1]
  resolved_correct              0 or 1

Records where either is None are skipped.

Brier score: (1/N) * Σ (p − outcome)^2, ranging [0, 1].
Normalised for the credibility UI: 1 − brier/0.25, clamped [0, 1].
0.25 is the worst Brier a random predictor achieves, so dividing by
0.25 gives a score where 1.0 = perfectly calibrated, 0.0 = anti-calibrated.

The ten-bin reliability diagram data is used by the source detail page
to show where a source is over- or under-confident.
"""

from __future__ import annotations

from typing import Any, Optional


MIN_SAMPLE = 10


def _valid_record(rec: Any) -> bool:
    if rec is None:
        return False
    try:
        prob = rec["predicted_probability_stated"] if isinstance(rec, dict) else getattr(rec, "predicted_probability_stated")
        outcome = rec["resolved_correct"] if isinstance(rec, dict) else getattr(rec, "resolved_correct")
    except (KeyError, AttributeError):
        return False
    if prob is None or outcome is None:
        return False
    try:
        prob = float(prob)
    except (TypeError, ValueError):
        return False
    if not (0.0 <= prob <= 1.0):
        return False
    if outcome not in (0, 1, True, False):
        return False
    return True


def _get(rec: Any, key: str) -> Any:
    if isinstance(rec, dict):
        return rec.get(key)
    return getattr(rec, key, None)


def brier_component_for_record(predicted_probability: float, resolved_correct: int) -> float:
    """(p − o)^2 — the per-record Brier contribution.

    Expose it so the pipeline can store a pre-computed ``calibration_contribution``
    per record, avoiding a re-scan over the whole history every recompute.
    """
    p = max(0.0, min(1.0, float(predicted_probability)))
    o = 1.0 if resolved_correct else 0.0
    return (p - o) ** 2


def compute_brier_score(records: list[Any]) -> Optional[dict]:
    """Normalised Brier score + sample size.

    Returns ``None`` when fewer than :data:`MIN_SAMPLE` (10) usable records —
    calibration isn't meaningful at small N. Otherwise returns::

        {
          "sample_size":  int,
          "brier":        float (0–1, lower is better),
          "calibration":  float (0–1, higher is better — the UI version),
        }
    """
    usable = [r for r in (records or []) if _valid_record(r)]
    if len(usable) < MIN_SAMPLE:
        return None
    total = 0.0
    for rec in usable:
        p = float(_get(rec, "predicted_probability_stated"))
        o = 1.0 if _get(rec, "resolved_correct") else 0.0
        total += (p - o) ** 2
    brier = total / len(usable)
    calibration = 1.0 - (brier / 0.25)
    calibration = max(0.0, min(1.0, calibration))
    return {
        "sample_size": len(usable),
        "brier": round(brier, 6),
        "calibration": round(calibration, 6),
    }


def reliability_diagram_data(records: list[Any], *, bins: int = 10) -> list[dict]:
    """Bucket records by stated probability and return per-bucket averages.

    Shape::

        [
          {
            "bin_lo":        float,    # inclusive lower edge
            "bin_hi":        float,    # exclusive upper edge (inclusive at 1.0)
            "count":         int,
            "predicted_avg": float,    # mean of stated probabilities in bucket
            "actual_accuracy": float,  # fraction of correct outcomes in bucket
            "is_overconfident":  bool, # predicted_avg - actual > 0.10
            "is_underconfident": bool, # actual - predicted_avg > 0.10
            "is_calibrated":     bool, # |delta| <= 0.10
          },
          ... bins entries
        ]

    Empty buckets are included for chart continuity.
    """
    bins = max(2, int(bins))
    usable = [r for r in (records or []) if _valid_record(r)]
    buckets: list[dict] = []
    for i in range(bins):
        lo = i / bins
        hi = (i + 1) / bins
        buckets.append({
            "bin_lo": round(lo, 4),
            "bin_hi": round(hi, 4),
            "count": 0,
            "_prob_sum": 0.0,
            "_correct_sum": 0,
        })

    for rec in usable:
        p = float(_get(rec, "predicted_probability_stated"))
        idx = min(int(p * bins), bins - 1)  # 1.0 falls in the last bucket
        b = buckets[idx]
        b["count"] += 1
        b["_prob_sum"] += p
        if _get(rec, "resolved_correct"):
            b["_correct_sum"] += 1

    out: list[dict] = []
    for b in buckets:
        if b["count"]:
            predicted_avg = b["_prob_sum"] / b["count"]
            actual = b["_correct_sum"] / b["count"]
        else:
            predicted_avg = (b["bin_lo"] + b["bin_hi"]) / 2.0
            actual = 0.0
        delta = predicted_avg - actual
        out.append({
            "bin_lo": b["bin_lo"],
            "bin_hi": b["bin_hi"],
            "count": b["count"],
            "predicted_avg": round(predicted_avg, 4),
            "actual_accuracy": round(actual, 4),
            "is_overconfident":  bool(b["count"]) and delta > 0.10,
            "is_underconfident": bool(b["count"]) and delta < -0.10,
            "is_calibrated":     bool(b["count"]) and abs(delta) <= 0.10,
        })
    return out
