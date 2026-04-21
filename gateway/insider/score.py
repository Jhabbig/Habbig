"""Insider signal scoring — pure functions.

``compute_insider_score`` is the spec-defined weighted sum:

    0.4 * signal_strength
  + 0.2 * disclosure_delay
  + 0.2 * amount_significance
  + 0.2 * correlation_confidence

Each input is normalised to [0, 1]. Strings get mapped through
``STRENGTH_VALUES`` / ``CONFIDENCE_VALUES``. Unknown values fall to 0.
"""

from __future__ import annotations

from typing import Any


STRENGTH_VALUES = {"strong": 1.0, "moderate": 0.6, "weak": 0.3}
CONFIDENCE_VALUES = {"high": 1.0, "medium": 0.65, "low": 0.35, "speculative": 0.15}


def compute_insider_score(
    *,
    signal_strength: Any,
    disclosure_delay: Any,
    amount_significance: Any,
    correlation_confidence: Any,
) -> float:
    strength = _from_enum(signal_strength, STRENGTH_VALUES)
    delay = _num01(disclosure_delay)
    amount = _num01(amount_significance)
    conf = _from_enum(correlation_confidence, CONFIDENCE_VALUES)
    score = (0.4 * strength) + (0.2 * delay) + (0.2 * amount) + (0.2 * conf)
    return max(0.0, min(1.0, round(score, 6)))


def _num01(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return max(0.0, min(1.0, float(val)))
    except (TypeError, ValueError):
        return 0.0


def _from_enum(val: Any, table: dict) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return _num01(val)
    key = str(val).strip().lower()
    return table.get(key, 0.0)
