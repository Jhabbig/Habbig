from __future__ import annotations
"""Movement analysis pipeline: price deltas + news + grounded LLM call.

The flow:
  1. Pull divergence history for the race over the requested window.
  2. Compute per-source price deltas.
  3. If movement is below the noise threshold, short-circuit — no LLM call.
  4. Pull the race's curated context (candidate names, lean) for query targeting.
  5. Fetch news articles from the same window via NewsAPI / GDELT.
  6. Call Claude with strict grounding rules + structured output schema.
  7. Cache the result for 1 hour per (race_key, hour_bucket).

The cache is critical for cost — without it, every page load on RaceDetail
would burn another LLM call. With it, we pay at most once per race per hour.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Movements smaller than this aren't worth explaining — within noise.
NOISE_THRESHOLD_PP = 1.5
EXPLANATION_TTL_SECONDS = 3600


def _hour_bucket(dt: datetime) -> str:
    """Cache key suffix that changes once an hour."""
    return dt.strftime("%Y-%m-%dT%H")


def _movements_from_history(history: list[dict], hours: int) -> list[dict]:
    """Compute per-source deltas from the divergence history rows."""
    if not history:
        return []
    history = sorted(history, key=lambda h: h.get("snapshot_time") or "")
    # Take only the rows within the window
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_iso = cutoff.isoformat()
    window = [h for h in history if (h.get("snapshot_time") or "") >= cutoff_iso] or history
    by_source: dict[str, list[float]] = {"polymarket": [], "kalshi": [], "predictit": [], "polling": []}
    for h in window:
        for s in by_source:
            v = h.get(f"{s}_prob")
            if v is not None:
                by_source[s].append(v)
    movements = []
    for s, vals in by_source.items():
        if len(vals) < 2:
            continue
        delta_pp = (vals[-1] - vals[0]) * 100
        movements.append({
            "source": s,
            "from": round(vals[0], 4),
            "to": round(vals[-1], 4),
            "delta_pp": round(delta_pp, 2),
        })
    movements.sort(key=lambda m: abs(m["delta_pp"]), reverse=True)
    return movements


def _max_abs_delta(movements: list[dict]) -> float:
    if not movements:
        return 0.0
    return max(abs(m["delta_pp"]) for m in movements)


async def analyze_movement(
    *,
    db,
    session: aiohttp.ClientSession,
    race_key: str,
    race_title: str,
    race_type: str,
    state: str,
    hours: int = 24,
    race_context: dict | None = None,
) -> dict:
    """Public entry point used by /data/race/{key}/movements.

    Returns a dict shaped for the API response::

        {
          "movements": [...],
          "candidates": [...],         # legacy field, kept for compat
          "articles": [...],           # raw articles passed to the LLM
          "explanation": {
              "summary": str,
              "explanations": [...],
              "reason_if_empty": str | None,
              "model": str | None,
              "usage": {...} | None,
              "configured": bool,
          },
          "cached": bool,
          "window_hours": int,
        }
    """
    if hours <= 0 or hours > 168:
        hours = 24

    # 1. Price history → deltas. Pull a slightly wider window for context.
    history = db.get_divergence_history(race_key=race_key, days=max(1, hours // 24 + 1))
    movements = _movements_from_history(history, hours)

    # 2. Cache lookup
    now = datetime.now(timezone.utc)
    bucket = f"{_hour_bucket(now)}_{hours}h"
    cached = db.get_movement_explanation(race_key, bucket)
    if cached:
        return {**cached, "cached": True, "window_hours": hours, "movements": movements}

    # 3. Short-circuit on insignificant movement
    if _max_abs_delta(movements) < NOISE_THRESHOLD_PP:
        result = {
            "movements": movements,
            "candidates": [{
                "type": "info",
                "headline": "Movement below threshold",
                "note": f"No source moved more than {NOISE_THRESHOLD_PP}pp in the window; nothing material to explain.",
            }],
            "articles": [],
            "explanation": {
                "summary": "",
                "explanations": [],
                "reason_if_empty": "insufficient_movement",
                "model": None,
                "usage": None,
                "configured": True,
            },
            "cached": False,
            "window_hours": hours,
        }
        db.store_movement_explanation(race_key, bucket, result, ttl_seconds=EXPLANATION_TTL_SECONDS)
        return result

    # 4. Fetch news in the same window
    from news import fetch_articles_for_race, channels_available
    articles = await fetch_articles_for_race(
        session,
        race_type=race_type,
        state=state,
        window_hours=hours,
        race_context=race_context,
        end_ts=now,
    )

    # 5. Ground + call Claude
    from llm import explain_movement, llm_configured
    from_ts = now - timedelta(hours=hours)
    explanation = await explain_movement(
        race_key=race_key,
        race_title=race_title or race_key,
        race_type=race_type,
        state=state,
        movements=movements,
        articles=articles,
        from_ts=from_ts,
        to_ts=now,
    )

    # 6. Build the candidates list for the legacy frontend shape
    legacy_candidates = []
    if not llm_configured():
        legacy_candidates.append({
            "type": "info",
            "headline": "Set ANTHROPIC_API_KEY to enable AI-summarized explanations",
            "note": "The endpoint will keep returning per-source deltas; news + LLM analysis activates once the key is configured.",
        })
    if not channels_available().get("newsapi") and not articles:
        legacy_candidates.append({
            "type": "info",
            "headline": "No news sources returned articles in this window",
            "note": "Set NEWS_API_KEY for richer article coverage; GDELT is used as a free fallback but is sparser.",
        })
    # Convert structured explanations into the legacy "candidates" shape so
    # older clients keep working unchanged.
    for exp in explanation.get("explanations", []):
        legacy_candidates.append({
            "type": "news",
            "headline": exp.get("headline", ""),
            "url": exp.get("url", ""),
            "quote": exp.get("quote", ""),
            "rationale": exp.get("rationale", ""),
            "confidence": exp.get("confidence", "low"),
        })
    if explanation.get("summary"):
        legacy_candidates.insert(0, {
            "type": "summary",
            "headline": explanation["summary"],
        })

    result = {
        "movements": movements,
        "candidates": legacy_candidates,
        "articles": articles,
        "explanation": explanation,
        "cached": False,
        "window_hours": hours,
    }
    db.store_movement_explanation(race_key, bucket, result, ttl_seconds=EXPLANATION_TTL_SECONDS)
    return result
