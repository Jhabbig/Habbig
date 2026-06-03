"""Anomaly index — how unusual is today vs the trailing 30-day distribution?

For each section we compute the z-score of today's score against the
trailing-window mean and stddev. The overall anomaly is the Euclidean
norm of those z-scores — a single number quantifying multi-dimensional
deviation:

    anomaly = sqrt(sum(z_i^2))

Rough reading:

    0-1   normal — typical day
    1-2   mild — somewhat unusual
    2-3   high — quite unusual
    3+    rare — sustained deviation across multiple sections

Returns null for sections with insufficient history; the index covers
only the sections that have enough data.
"""

from __future__ import annotations

import math
import os
from typing import Any

import cache
from index_calc import CALIBRATION


def _min_points() -> int:
    try:
        return int(os.environ.get("CULTURE_ANOMALY_MIN_POINTS", "12"))
    except ValueError:
        return 12


def _window_hours() -> int:
    try:
        return int(os.environ.get("CULTURE_ANOMALY_WINDOW_HOURS", "720"))
    except ValueError:
        return 720


def compute() -> dict[str, Any]:
    history = cache.index_history(hours=_window_hours())
    min_pts = _min_points()
    if len(history) < min_pts:
        return {
            "anomaly": None,
            "components": {},
            "history_points": len(history),
            "needs_at_least": min_pts,
        }

    components: dict[str, float | None] = {}
    sum_sq = 0.0
    contributing = 0
    for section in CALIBRATION.keys():
        scores = [p.get("sections", {}).get(section, {}).get("score")
                  for p in history]
        scores = [s for s in scores if s is not None]
        if len(scores) < min_pts:
            components[section] = None
            continue
        current = scores[-1]
        trailing = scores[:-1]
        if not trailing:
            components[section] = None
            continue
        mean = sum(trailing) / len(trailing)
        var = sum((s - mean) ** 2 for s in trailing) / len(trailing)
        std = math.sqrt(var)
        if std < 1e-6:
            components[section] = 0.0
            continue
        z = (current - mean) / std
        components[section] = round(z, 2)
        sum_sq += z * z
        contributing += 1

    anomaly = round(math.sqrt(sum_sq), 2) if contributing else None
    return {
        "anomaly": anomaly,
        "components": components,
        "history_points": len(history),
        "contributing_sections": contributing,
        "window_hours": _window_hours(),
    }
