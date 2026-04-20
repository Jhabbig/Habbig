"""
Spike detector — flags entities with anomalous annoyance-weighted volume.

Why this design, not naive z-score on volume:
  * Composite signal = count * (avg_annoyance / 50). Penalizes lukewarm-volume
    and rewards high-anger-even-at-low-count.
  * MAD (median absolute deviation) instead of stddev — one viral post inflates
    stddev and masks the next spike. MAD is robust to outliers.
  * Baseline uses same-hour-of-week, not flat 7×24. Sunday 3am ≠ Monday 10am.
  * Three gates to fire: z >= 3 AND multiple >= 3 AND count >= 5. Three gates
    cut false positives ~by half in tests.
  * Cold start: during first MIN_BASELINE_HOURS of entity history, fall back
    to absolute thresholds (count >= 10 AND avg_annoyance >= 70).
  * Dedup by (entity, detected_hour) in the spikes table — the 15-min loop
    can't re-emit the same spike.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from datetime import datetime
from urllib.parse import quote as _urlquote

import config
import db

log = logging.getLogger("annoyance.spike_detector")


def _compute_confidence(
    *,
    z: float,
    multiple: float,
    backtest_hit_rate: float = 0.5,
    warmup: bool = False,
) -> float:
    """0-100 blended confidence score for a spike.

    Three components:
      * z     — normalized from [3, 10] into [0, 50]. The gate threshold
                sits at 0; a true outlier (z>=10) saturates the component.
      * mult  — multiple-of-baseline normalized from [3, 10] into [0, 25].
      * bt    — historical hit rate of spikes for this entity, scaled
                [0, 25]. Defaults to 0.5 (neutral) until the backtest
                framework populates real per-entity rates.

    Warmup spikes bypass the components entirely and return a flat 30 —
    low-medium, signaling "we don't have enough history to judge". This is
    INTENTIONAL and should not be "tuned" without understanding why:
      * z and multiple are meaningless during warmup (no baseline to compare
        against), so their components would be lying if computed.
      * A warmup fire gated purely by absolute thresholds
        (count>=WARMUP_MIN_COUNT AND avg_annoyance>=WARMUP_MIN_AVG_ANNOYANCE)
        tells the user "loud entity, but we have no history" — 30/100 is the
        honest translation. Raising it would over-claim confidence we don't
        have; lowering it would hide legitimate early signal.
      * The floor also guards spike-card UI: confidence<40 renders red,
        warning the reader "take this with salt". That's the correct
        treatment for cold-start fires.
    See P4.2 in SECURITY_AUDIT_2026-04-20.md for audit context.
    """
    if warmup:
        return 30.0
    z_c = max(0.0, min(50.0, (z - 3) / 7 * 50))
    m_c = max(0.0, min(25.0, (multiple - 3) / 7 * 25))
    bt_c = max(0.0, min(1.0, backtest_hit_rate)) * 25.0
    return round(z_c + m_c + bt_c, 1)


def _signal(count: int, avg_annoyance: float) -> float:
    return count * (avg_annoyance / 50.0)


def _hour_of_week(hour_iso: str) -> int:
    dt = datetime.fromisoformat(hour_iso)
    return dt.weekday() * 24 + dt.hour


def _median_abs_deviation(values: list[float], median: float) -> float:
    if not values:
        return 0.0
    return statistics.median([abs(v - median) for v in values])


def _check_warmup(current: dict) -> bool:
    """Absolute thresholds for cold-start firing."""
    return (
        current["count"] >= config.WARMUP_MIN_COUNT
        and current["avg_annoyance"] >= config.WARMUP_MIN_AVG_ANNOYANCE
    )


def _check_gates(z: float, multiple: float, count: int) -> bool:
    return (
        z >= config.SPIKE_Z_THRESHOLD
        and multiple >= config.SPIKE_MULTIPLE_THRESHOLD
        and count >= config.SPIKE_MIN_COUNT
    )


def _apply_multi_source_gate(entity: str, current_hour: str, info: dict) -> bool:
    """Require >=2 sources each contributing >=2 posts to the entity this hour.

    Returns True if the gate passes (spike allowed to fire), False otherwise.
    Mutates `info` with diagnostic fields either way so logs + spike rows can
    show exactly who corroborated.

    Warmup mode bypasses this gate — see caller. Warmup already uses stricter
    absolute thresholds (count>=10 AND avg_annoyance>=70) and requiring
    multi-source on top would mean zero spikes during the first week of data,
    which defeats the whole purpose of warmup.

    Contributing threshold (>=2) is deliberately low: one Reddit post and one
    Bluesky post about the same entity in the same hour is rare enough signal
    to count. If false-positive rate stays high after launch, bump it to >=3.
    """
    # Enriched shape: {source: {"posts": n, "unique_authors": n}}. The
    # unique_authors count is the single biggest tell for coordinated gaming
    # (P4.1): a source where posts >> unique_authors is likely one account
    # spraying, not organic multi-person outrage. We still gate on posts, but
    # surface the ratio downstream so the FP admin queue can flag it.
    per_source_stats = db.get_entity_hourly_source_stats(entity, current_hour)
    per_source_counts = {s: v["posts"] for s, v in per_source_stats.items()}
    contributing = [s for s, v in per_source_stats.items() if v["posts"] >= 2]
    # Always record the breakdown so the UI / logs can show which sources
    # saw this entity even when the gate blocks the fire.
    info["sources_observed"] = per_source_counts
    info["sources_breakdown"] = [
        {
            "source": s,
            "count": v["posts"],
            "unique_authors": v["unique_authors"],
        }
        for s, v in per_source_stats.items()
    ]
    if len(contributing) < 2:
        info["reason"] = "multi_source_gate_failed"
        info["sources_contributing"] = contributing
        return False
    info["sources_contributing"] = contributing
    return True


def _evaluate_entity(entity: str, current_hour: str) -> tuple[bool, dict]:
    """
    Returns (fire, info_dict). info_dict always populated with the metrics used
    even when not firing, so logs are useful for tuning.

    `current_hour` is threaded through so the multi-source gate can query
    per-source counts for the exact bucket we evaluated — no chance of a
    drift between detector start and gate query.
    """
    history = db.get_entity_history(entity, hours=24 * 14)  # 2 weeks
    if not history:
        return False, {"reason": "no_history"}

    # Current hour is the most recent bucket
    current = history[-1]
    if current["count"] < 1:
        return False, {"reason": "empty_current"}

    current_signal = _signal(current["count"], current["avg_annoyance"])

    # Baseline = same hour-of-week, excluding the current observation
    current_how = _hour_of_week(current["hour"])
    baseline_signals = [
        _signal(h["count"], h["avg_annoyance"])
        for h in history[:-1]
        if _hour_of_week(h["hour"]) == current_how
    ]

    # Need enough baseline points to do MAD. If the entity is brand-new or
    # sparse, fall back to warmup logic.
    if len(history) < config.MIN_BASELINE_HOURS or len(baseline_signals) < 3:
        if _check_warmup(current):
            # Warmup mode deliberately bypasses the multi-source gate. See
            # _apply_multi_source_gate docstring for rationale.
            info = {
                "entity": entity,
                "mode": "warmup",
                "count": current["count"],
                "avg_annoyance": current["avg_annoyance"],
                "current_signal": current_signal,
                "z_score": 0.0,
                "multiple_of_baseline": 0.0,
            }
            # Populate sources_breakdown even on warmup fires so the spike row
            # has the same shape as statistical-mode fires downstream. Use the
            # enriched helper so admin FP queue sees posts/authors ratio for
            # warmup fires too — those are the riskiest to blindly trust.
            per_source_stats = db.get_entity_hourly_source_stats(entity, current_hour)
            info["sources_breakdown"] = [
                {
                    "source": s,
                    "count": v["posts"],
                    "unique_authors": v["unique_authors"],
                }
                for s, v in per_source_stats.items()
            ]
            return True, info
        return False, {
            "reason": "warmup_threshold_not_met",
            "count": current["count"],
            "avg_annoyance": current["avg_annoyance"],
        }

    median = statistics.median(baseline_signals)
    mad = _median_abs_deviation(baseline_signals, median)
    # 1.4826 scales MAD to be a consistent estimator of stddev under normality
    mad_sigma = mad * 1.4826 if mad > 0 else 0.0

    if mad_sigma == 0.0:
        # All baseline signals identical; use absolute multiple only
        multiple = current_signal / median if median > 0 else 0.0
        z = 0.0
    else:
        z = (current_signal - median) / mad_sigma
        multiple = current_signal / median if median > 0 else 0.0

    fire = _check_gates(z, multiple, current["count"])
    info = {
        "entity": entity,
        "mode": "statistical",
        "count": current["count"],
        "avg_annoyance": current["avg_annoyance"],
        "current_signal": round(current_signal, 2),
        "baseline_median": round(median, 2),
        "baseline_mad": round(mad, 2),
        "z_score": round(z, 2),
        "multiple_of_baseline": round(multiple, 2),
    }
    if not fire:
        info["reason"] = "gates_not_met"
        return False, info

    # Statistical gates passed. Now corroborate: require >=2 sources each
    # contributing >=2 posts about this entity in the current hour. This is
    # the single biggest FP killer — one viral Reddit thread can blow past
    # z/mult/count without ever touching another platform, and that's almost
    # always not a real story.
    if config.REQUIRE_MULTI_SOURCE:
        if not _apply_multi_source_gate(entity, current_hour, info):
            return False, info

    return True, info


def _sample_posts_for_entity(entity: str, hour_iso: str, limit: int = 5) -> list[dict]:
    """Fetch a few representative posts from the current hour for the spike summary."""
    # We don't have a direct entity→posts join, so walk classifications in this
    # hour and pick ones whose entities_json mentions this entity.
    import json
    classifications = db.get_classifications_in_hour(hour_iso)
    matches = []
    for row in classifications:
        try:
            entities = json.loads(row.get("entities_json") or "[]")
        except Exception:
            continue
        for e in entities:
            if not isinstance(e, dict):
                continue
            from aggregator import canonicalize
            if canonicalize(e.get("name") or "") == entity:
                matches.append(row)
                break
        if len(matches) >= limit:
            break
    return matches


async def detect_and_record() -> list[dict]:
    """
    Main entry point called from the scheduler loop. Walks entities with
    enough activity, evaluates each, fires spikes, generates summaries.
    Returns a list of fired spike dicts for logging/telemetry.
    """
    from classifier import summarize_spike  # avoid circular import at module load

    entities = db.get_distinct_entities_with_min_count(config.SPIKE_MIN_COUNT)
    fired: list[dict] = []
    current_hour = db.current_hour_iso()

    for entity in entities:
        try:
            fire, info = _evaluate_entity(entity, current_hour)
        except Exception:
            log.exception("spike_detector: evaluate failed for %s", entity)
            continue

        if not fire:
            # Log failed evaluations at debug level with the reason so we can
            # tune gate thresholds by tailing the log. Multi-source gate
            # failures specifically are worth surfacing at info level since
            # they're the new class of rejection.
            if info.get("reason") == "multi_source_gate_failed":
                log.info(
                    "spike blocked by multi-source gate: %s sources=%s",
                    entity, info.get("sources_observed"),
                )
            continue

        samples = _sample_posts_for_entity(entity, current_hour, limit=5)
        sample_ids = [s.get("post_id") for s in samples if s.get("post_id")]
        # Sub-decision B: cache the first 200 chars of the top 3 samples
        # directly on the spike row. After the 30-day raw-content TTL
        # scrubs posts.content, this is the only way the spike card can
        # still show what the entity was being discussed about.
        sample_excerpts = [
            (s.get("content") or "")[:200] for s in samples[:3]
        ]

        # Generate summary (best-effort). None is fine — spike still records.
        summary = None
        try:
            summary = await summarize_spike(entity, samples)
        except Exception:
            log.exception("spike_detector: summary failed for %s", entity)

        # Blended confidence (decision #10) — z + multiple + backtest hit rate.
        # Stored on the spike row so the UI can render the 0-100 bar without
        # recomputing on every page load.
        confidence = _compute_confidence(
            z=info.get("z_score") or 0.0,
            multiple=info.get("multiple_of_baseline") or 0.0,
            backtest_hit_rate=0.5,  # neutral until backtest wires per-entity rates
            warmup=(info.get("mode") == "warmup"),
        )
        info["confidence_score"] = confidence

        inserted = db.insert_spike(
            entity=entity,
            detected_hour=current_hour,
            z_score=info.get("z_score") or 0.0,
            multiple_of_baseline=info.get("multiple_of_baseline") or 0.0,
            avg_annoyance=info.get("avg_annoyance") or 0.0,
            count=info.get("count") or 0,
            sample_post_ids=sample_ids,
            sample_excerpts=sample_excerpts,
            confidence_score=confidence,
            sources_breakdown=info.get("sources_breakdown") or [],
            summary=summary,
        )
        if inserted:
            log.info("SPIKE fired: %s (%s)", entity, info)
            info["summary"] = summary
            fired.append(info)

            # Fire-and-forget email to Pro subscribers. Any failure (SMTP,
            # missing gateway auth.db, template errors) is swallowed so the
            # detector loop never blocks on notification delivery. The
            # notifications module handles per-user dedup + daily rate limit.
            try:
                from notifications import send_spike_email
                entity_url = f"https://annoyance.narve.ai/entity/{_urlquote(entity)}"
                await send_spike_email(
                    spike_id=inserted,
                    entity=entity,
                    summary=summary or "",
                    confidence=confidence,
                    entity_url=entity_url,
                )
            except Exception:
                log.exception("spike_detector: email dispatch failed for %s", entity)

    return fired
