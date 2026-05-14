"""
Happiness spike detector — the positive-polarity sibling of spike_detector.py.

Decision #7 (2026-05-14 unlock): one product, two views. The annoyance side
fires on volume × anger; the happiness side fires on volume × *positive*
classifier sentiment. Same statistical machinery — z-score over hour-of-week
baseline, MAD instead of stddev — but the input stream is
``classifications.sentiment = 'positive'`` rows instead of all rows.

Why no separate classifier:
- ``classifier.CLASSIFY_SYSTEM_PROMPT`` already emits ``sentiment`` ∈
  {angry, frustrated, neutral, positive}. We treat 'positive' as the
  happiness-polarity signal and derive ternary polarity:
    sentiment ∈ {angry, frustrated} → polarity 'negative'
    sentiment == 'positive'          → polarity 'positive'
    sentiment == 'neutral'           → polarity 'neutral'
  This is the "ternary classifier" called out in DECISIONS.md #7 — same
  Claude model, same prompt, no incremental cost.
- No re-classification of historical posts is needed. The classifier has
  been emitting 'positive' since day-one; the data is already there.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime
from typing import Optional

import config
import db

log = logging.getLogger("annoyance.happiness")


# Reuse the annoyance thresholds for the positive side. Tunable via config
# overrides if the FP rate diverges from the annoyance side after launch.
POSITIVE_Z_THRESHOLD = getattr(config, "HAPPINESS_Z_THRESHOLD", config.SPIKE_Z_THRESHOLD)
POSITIVE_MULTIPLE_THRESHOLD = getattr(
    config, "HAPPINESS_MULTIPLE_THRESHOLD", config.SPIKE_MULTIPLE_THRESHOLD
)
POSITIVE_MIN_COUNT = getattr(config, "HAPPINESS_MIN_COUNT", config.SPIKE_MIN_COUNT)


def _signal(count: int, avg_score: float) -> float:
    """Volume × positivity. Reuses annoyance_score as a generic intensity dial."""
    return count * (max(avg_score, 1.0) / 50.0)


def _hour_of_week(hour_iso: str) -> int:
    dt = datetime.fromisoformat(hour_iso)
    return dt.weekday() * 24 + dt.hour


def _mad(values: list[float], median: float) -> float:
    if not values:
        return 0.0
    return statistics.median([abs(v - median) for v in values])


def _evaluate_positive_entity(
    entity: str, current_hour: str
) -> tuple[bool, dict]:
    """Returns (fire, info). info is populated even when not firing so logs
    are tunable. Mirrors ``spike_detector._evaluate_entity`` but operates on
    positive-sentiment rows.
    """
    positive_now = db.get_positive_classifications_in_hour(current_hour)
    if not positive_now:
        return False, {"reason": "no_positive_classifications_this_hour"}

    from aggregator import canonicalize
    matched_posts: list[dict] = []
    score_sum = 0.0
    for row in positive_now:
        try:
            ents = json.loads(row.get("entities_json") or "[]")
        except Exception:
            continue
        for e in ents:
            if not isinstance(e, dict):
                continue
            name = (e.get("name") or "").strip()
            if not name:
                continue
            if canonicalize(name) != entity:
                continue
            esent = e.get("sentiment") or "positive"
            if esent in ("angry", "frustrated"):
                continue
            matched_posts.append(row)
            score_sum += float(row.get("annoyance_score") or 0.0)
            break

    cnt = len(matched_posts)
    if cnt < POSITIVE_MIN_COUNT:
        return False, {
            "reason": "below_min_count",
            "count": cnt,
            "threshold": POSITIVE_MIN_COUNT,
        }

    avg_score = score_sum / cnt if cnt else 0.0
    current_signal = _signal(cnt, avg_score)

    history = db.get_entity_history(entity, hours=24 * 14)
    current_how = _hour_of_week(current_hour)
    baseline_signals = [
        _signal(h["count"], h["avg_annoyance"])
        for h in history
        if _hour_of_week(h["hour"]) == current_how and h["hour"] != current_hour
    ]

    if len(history) < config.MIN_BASELINE_HOURS or len(baseline_signals) < 3:
        if cnt >= POSITIVE_MIN_COUNT * 2 and avg_score >= 40:
            return True, {
                "entity": entity, "mode": "warmup", "count": cnt,
                "avg_score": round(avg_score, 2),
                "current_signal": round(current_signal, 2),
                "z_score": 0.0, "multiple_of_baseline": 0.0,
                "polarity": "positive",
            }
        return False, {
            "reason": "warmup_threshold_not_met",
            "count": cnt, "avg_score": avg_score,
        }

    median = statistics.median(baseline_signals)
    mad_val = _mad(baseline_signals, median)
    mad_sigma = mad_val * 1.4826 if mad_val > 0 else 0.0
    if mad_sigma == 0.0:
        multiple = current_signal / median if median > 0 else 0.0
        z = 0.0
    else:
        z = (current_signal - median) / mad_sigma
        multiple = current_signal / median if median > 0 else 0.0

    fire = (
        z >= POSITIVE_Z_THRESHOLD
        and multiple >= POSITIVE_MULTIPLE_THRESHOLD
        and cnt >= POSITIVE_MIN_COUNT
    )
    info = {
        "entity": entity, "mode": "statistical", "count": cnt,
        "avg_score": round(avg_score, 2),
        "current_signal": round(current_signal, 2),
        "baseline_median": round(median, 2),
        "baseline_mad": round(mad_val, 2),
        "z_score": round(z, 2),
        "multiple_of_baseline": round(multiple, 2),
        "polarity": "positive",
    }
    return fire, info


def _confidence(z: float, multiple: float, warmup: bool) -> float:
    """Reuse the same blended confidence shape as spike_detector for UI consistency."""
    if warmup:
        return 30.0
    z_c = max(0.0, min(50.0, (z - 3) / 7 * 50))
    m_c = max(0.0, min(25.0, (multiple - 3) / 7 * 25))
    bt_c = 0.5 * 25.0  # neutral until per-entity happiness backtest exists
    return round(z_c + m_c + bt_c, 1)


async def detect_and_record() -> list[dict]:
    """Main entry point — mirrors ``spike_detector.detect_and_record`` but
    walks positive-sentiment classifications and inserts spikes with
    polarity='positive'.
    """
    current_hour = db.current_hour_iso()
    entities = db.get_distinct_entities_with_min_count(POSITIVE_MIN_COUNT)
    fired: list[dict] = []

    for entity in entities:
        try:
            fire, info = _evaluate_positive_entity(entity, current_hour)
        except Exception:
            log.exception("happiness: evaluate failed for %s", entity)
            continue
        if not fire:
            continue

        positive_now = db.get_positive_classifications_in_hour(current_hour)
        from aggregator import canonicalize
        samples: list[dict] = []
        for row in positive_now:
            try:
                ents = json.loads(row.get("entities_json") or "[]")
            except Exception:
                continue
            if any(
                isinstance(e, dict)
                and canonicalize(e.get("name") or "") == entity
                for e in ents
            ):
                samples.append(row)
            if len(samples) >= 5:
                break

        sample_ids = [s.get("post_id") for s in samples if s.get("post_id")]
        sample_excerpts = [(s.get("content") or "")[:200] for s in samples[:3]]
        confidence = _confidence(
            z=info.get("z_score") or 0.0,
            multiple=info.get("multiple_of_baseline") or 0.0,
            warmup=(info.get("mode") == "warmup"),
        )
        summary = info.get("summary") or f"Positive-sentiment spike on {entity}"

        spike_id = db.insert_spike(
            entity=entity, detected_hour=current_hour,
            z_score=info.get("z_score") or 0.0,
            multiple_of_baseline=info.get("multiple_of_baseline") or 0.0,
            avg_annoyance=info.get("avg_score") or 0.0,
            count=info.get("count") or 0,
            sample_post_ids=sample_ids,
            sample_excerpts=sample_excerpts,
            confidence_score=confidence,
            sources_breakdown=[],
            summary=summary,
            polarity="positive",
        )
        if spike_id:
            info["confidence_score"] = confidence
            info["summary"] = summary
            info["spike_id"] = spike_id
            fired.append(info)
            log.info("HAPPINESS SPIKE fired: %s (%s)", entity, info)

    return fired


def recent_happiness_spikes(limit: int = 20) -> list[dict]:
    """Convenience wrapper around ``db.get_recent_spikes(polarity='positive')``.
    Hydrates ``sample_posts`` for client rendering.
    """
    spikes = db.get_recent_spikes(limit=limit, polarity="positive")
    for s in spikes:
        ids = s.get("sample_post_ids") or []
        try:
            s["sample_posts"] = db.get_posts_with_sensitivity(ids)[:3]
        except Exception:
            s["sample_posts"] = []
        s.setdefault("polarity", "positive")
    return spikes


def top_happiness_entities(limit: int = 20, days: int = 30, min_mentions: int = 5) -> list[dict]:
    """Convenience wrapper around ``db.get_top_positive_entities``."""
    return db.get_top_positive_entities(
        min_mentions=min_mentions, days=days, limit=limit,
    )
