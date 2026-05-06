"""Surge detection.

For each item that has at least N prior history points we compute the
z-score of its current score against the trailing window. Items with
z above a threshold count as "surging" — meaning attention is climbing
faster than that item's own normal volatility.

Only useful for sources whose items recur across sweeps (Spotify, Wikipedia,
NYT bestsellers, Lyst, Steam, Apple Music, Pinterest, Polymarket markets).
One-shot sources (Reddit posts, headlines, KYM new entries) won't generate
surges — that's correct: there's no baseline to compare against.
"""

from __future__ import annotations

import math
import os
from typing import Any

import cache

MIN_HISTORY_POINTS = 4    # need ≥4 prior data points before z-score is meaningful
WINDOW_HOURS = 168        # 7 days of trailing data


def _zscore(current: float, history: list[float]) -> float | None:
    if len(history) < 2:
        return None
    mean = sum(history) / len(history)
    var = sum((x - mean) ** 2 for x in history) / len(history)
    std = math.sqrt(var)
    if std < 1e-9:
        return None
    return (current - mean) / std


def compute(limit: int = 20) -> list[dict[str, Any]]:
    rows = cache.items_with_history(min_points=MIN_HISTORY_POINTS, hours=WINDOW_HOURS)
    surges: list[dict[str, Any]] = []
    for item in rows:
        history = item.pop("history", [])
        if len(history) < MIN_HISTORY_POINTS:
            continue
        # Drop the most recent point if it equals the current row, then
        # compute z against the prior trailing window.
        prior = [h["score"] for h in history[:-1]] if len(history) > MIN_HISTORY_POINTS else [h["score"] for h in history]
        current = float(item.get("score") or 0)
        z = _zscore(current, prior)
        if z is None or z <= 0:
            continue
        item["z_score"] = round(z, 2)
        item["trajectory"] = history
        surges.append(item)

    surges.sort(key=lambda i: i["z_score"], reverse=True)
    return surges[:limit]


def webhook_threshold() -> float:
    try:
        return float(os.environ.get("SURGE_Z_THRESHOLD", "2.5"))
    except ValueError:
        return 2.5


def cooldown_seconds() -> float:
    try:
        return float(os.environ.get("SURGE_ALERT_COOLDOWN_HOURS", "6")) * 3600
    except ValueError:
        return 6 * 3600
