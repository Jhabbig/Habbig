"""Composite Culture Index.

A single 0-100 number per section plus a weighted overall index. The math
is intentionally simple — log of total signal volume (with a per-section
ceiling so a viral TikTok doesn't drown out everything else).

If a section has no data we report `null` rather than 0, so the UI can
distinguish "quiet" from "broken".
"""

from __future__ import annotations

import math
from typing import Any

import cache

# Per-section calibration: (max-volume saturation, weight in composite).
# These are rough but tunable; the dashboard exposes them as `extra` so we
# can tweak without redeploys later.
CALIBRATION: dict[str, tuple[float, float]] = {
    "memes":         (5_000_000_000, 0.25),
    "attention":     (1_000_000_000, 0.20),
    "entertainment": (   500_000_000, 0.15),
    "markets":       (    50_000_000, 0.10),
    "news":          (         1_000, 0.15),
    "language":      (        10_000, 0.05),
    "lifestyle":     (         5_000, 0.10),
}


def compute() -> dict[str, Any]:
    sections: dict[str, dict] = {}
    weighted_sum = 0.0
    weight_total = 0.0

    for section, (saturation, weight) in CALIBRATION.items():
        rows = cache.get_section(section, limit=200)
        if not rows:
            sections[section] = {"score": None, "items": 0, "total_signal": 0.0}
            continue
        total = sum(float(r.get("score") or 0) for r in rows)
        # log scale, normalised so saturation maps to 100
        if total <= 0:
            score = 0.0
        else:
            score = min(100.0, 100.0 * math.log1p(total) / math.log1p(saturation))
        sections[section] = {
            "score": round(score, 1),
            "items": len(rows),
            "total_signal": total,
        }
        weighted_sum += score * weight
        weight_total += weight

    overall = round(weighted_sum / weight_total, 1) if weight_total else None
    return {
        "overall": overall,
        "sections": sections,
        "calibration": {s: {"saturation": sat, "weight": w}
                        for s, (sat, w) in CALIBRATION.items()},
    }
