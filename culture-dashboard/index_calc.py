"""Composite Culture Index.

A single 0-100 number per section plus a weighted overall index. The math
is intentionally simple — log of total signal volume (with a per-section
ceiling so a viral TikTok doesn't drown out everything else).

The section weights are further multiplied by a **source-quality factor**:
sections fed by sources with proven predictive hit rate get amplified
(capped at 1.5x); sections fed by under-performing sources get dampened
(floored at 0.5x). The factor is cached in-process for 5 minutes so
/api/index doesn't have to re-walk the snapshot history on every page
load.

If a section has no data we report `null` rather than 0, so the UI can
distinguish "quiet" from "broken".
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

import cache

# Per-section calibration: (max-volume saturation, base-weight in composite).
CALIBRATION: dict[str, tuple[float, float]] = {
    "memes":         (5_000_000_000, 0.25),
    "attention":     (1_000_000_000, 0.20),
    "entertainment": (   500_000_000, 0.15),
    "markets":       (    50_000_000, 0.10),
    "news":          (         1_000, 0.15),
    "language":      (        10_000, 0.05),
    "lifestyle":     (         5_000, 0.10),
}

# In-process cache for source-quality multipliers (avoid per-request recompute).
_QUALITY_CACHE: dict[str, Any] = {"ts": 0.0, "multipliers": None, "raw": None}
_QUALITY_TTL = 300  # 5 minutes


def _source_to_section() -> dict[str, str]:
    """Build a source-name → section mapping from the scrapers registry."""
    # Local import — avoids circularity at module load time.
    from scrapers import registry
    import scrapers as _scrapers_pkg
    # registry() returns (name, fetch, period) but we need the section too,
    # which lives on the module that exposes NAME/SECTION constants.
    mapping = {}
    seen_modules = set()
    # Re-import the modules listed in registry() to read their SECTION constant.
    for mod_name in dir(_scrapers_pkg):
        mod = getattr(_scrapers_pkg, mod_name, None)
        if mod is None or mod_name in seen_modules:
            continue
        seen_modules.add(mod_name)
        if hasattr(mod, "NAME") and hasattr(mod, "SECTION"):
            mapping[mod.NAME] = mod.SECTION
    return mapping


def _quality_multiplier_for(rates_by_source: dict[str, float],
                            sources: list[str], alpha: float = 0.5) -> float:
    """Centred boost: at hit_rate 0.5 → 1.0; 1.0 → 1.5; 0.0 → 0.5."""
    samples = [rates_by_source[s] for s in sources if s in rates_by_source]
    if not samples:
        return 1.0
    mean = sum(samples) / len(samples)
    return max(0.5, min(1.5, 1.0 + alpha * (mean - 0.5)))


def effective_calibration() -> dict[str, dict]:
    """Return {section: {saturation, base_weight, quality_multiplier, weight}}.

    The cache key is just elapsed wall time; we don't invalidate on writes
    because the input is bulk historical data that changes slowly.
    """
    now = time.time()
    if (_QUALITY_CACHE["multipliers"] is not None
            and now - _QUALITY_CACHE["ts"] < _QUALITY_TTL):
        return _QUALITY_CACHE["multipliers"]

    out: dict[str, dict] = {}
    try:
        import source_quality
        sq = source_quality.compute(days=30)
        rates = {s["source"]: s["hit_rate"] for s in sq["sources"]
                 if s["hit_rate"] is not None}
    except Exception:  # noqa: BLE001
        rates = {}

    src_to_section = _source_to_section()
    section_sources: dict[str, list[str]] = {s: [] for s in CALIBRATION}
    for src, sec in src_to_section.items():
        if sec in section_sources:
            section_sources[sec].append(src)

    alpha = float(os.environ.get("CULTURE_QUALITY_WEIGHT_ALPHA", "0.5"))
    for section, (saturation, base_w) in CALIBRATION.items():
        mult = _quality_multiplier_for(rates, section_sources.get(section, []), alpha)
        out[section] = {
            "saturation": saturation,
            "base_weight": base_w,
            "quality_multiplier": round(mult, 3),
            "weight": round(base_w * mult, 4),
            "sources_considered": section_sources.get(section, []),
            "sources_with_data": [s for s in section_sources.get(section, [])
                                  if s in rates],
        }
    _QUALITY_CACHE.update({"ts": now, "multipliers": out, "raw": rates})
    return out


def compute() -> dict[str, Any]:
    sections: dict[str, dict] = {}
    calibration = effective_calibration()
    weighted_sum = 0.0
    weight_total = 0.0

    for section, cal in calibration.items():
        rows = cache.get_section(section, limit=200)
        if not rows:
            sections[section] = {"score": None, "items": 0, "total_signal": 0.0}
            continue
        total = sum(float(r.get("score") or 0) for r in rows)
        saturation = cal["saturation"]
        if total <= 0:
            score = 0.0
        else:
            score = min(100.0, 100.0 * math.log1p(total) / math.log1p(saturation))
        sections[section] = {
            "score": round(score, 1),
            "items": len(rows),
            "total_signal": total,
        }
        weighted_sum += score * cal["weight"]
        weight_total += cal["weight"]

    overall = round(weighted_sum / weight_total, 1) if weight_total else None
    return {
        "overall": overall,
        "sections": sections,
        "calibration": calibration,
    }
