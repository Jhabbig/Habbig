"""
Aggregator — rebuilds hourly annoyance index + per-entity counts.

Runs idempotently. Every tick it recomputes the current hour and the previous
hour (covers late-arriving classifications at hour boundaries). Upserts into
annoyance_index and entity_counts so reruns are safe.

Entity normalization via config.ALIASES — crude dict-lookup canonicalization.
Without it, "United"/"United Airlines"/"@united"/"UAL" fragment into 4 rows
and the spike detector never fires.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Optional

import config
import db

log = logging.getLogger("annoyance.aggregator")


def canonicalize(entity_name: str) -> str:
    """Apply ALIASES dict. Falls back to title-cased original."""
    key = entity_name.strip().lower()
    if key in config.ALIASES:
        return config.ALIASES[key]
    # Mild fallback normalization: strip leading '@', title case if lowercase.
    stripped = key.lstrip("@").strip()
    if stripped in config.ALIASES:
        return config.ALIASES[stripped]
    return entity_name.strip()


def rebuild_hour(hour_iso: str) -> dict:
    """
    Rebuild aggregate index + entity_counts for one hour bucket.
    Returns a summary dict for logging.

    Empty hours are REMOVED from annoyance_index and entity_counts rather
    than being written as zeros. A zero row would cause every hour-boundary
    to show as a fake dip-to-zero on the chart and would make
    /api/entities/top return nothing in the first minutes of each hour.
    """
    classifications = db.get_classifications_in_hour(hour_iso)
    if not classifications:
        with db.cursor() as cur:
            cur.execute("DELETE FROM annoyance_index WHERE hour = ?", (hour_iso,))
            cur.execute("DELETE FROM entity_counts WHERE hour = ?", (hour_iso,))
        return {"hour": hour_iso, "posts": 0, "entities": 0, "index": 0.0}

    total_score = 0.0
    post_count = 0
    per_source_scores: dict[str, list[float]] = defaultdict(list)
    # Collapse entities by canonical name. Track {canonical: [(score, type), ...]}
    entity_rows: dict[str, list[tuple[float, Optional[str]]]] = defaultdict(list)

    for row in classifications:
        score = float(row.get("annoyance_score") or 0.0)
        total_score += score
        post_count += 1
        per_source_scores[row.get("source") or "unknown"].append(score)

        try:
            entities = json.loads(row.get("entities_json") or "[]")
        except Exception:
            entities = []

        for e in entities:
            if not isinstance(e, dict):
                continue
            name = (e.get("name") or "").strip()
            if not name:
                continue
            canonical = canonicalize(name)
            # Per-entity "felt annoyance" = post score weighted by salience.
            # Use explicit None check — `or 0.5` would turn an explicit 0.0
            # into 0.5, defeating the floor below.
            raw_sal = e.get("salience")
            if raw_sal is None:
                salience = 0.5
            else:
                try:
                    salience = float(raw_sal)
                except (TypeError, ValueError):
                    salience = 0.5
            weighted = score * max(0.3, salience)  # floor so salience=0 doesn't zero it
            entity_rows[canonical].append((weighted, e.get("type")))

    avg_index = total_score / post_count if post_count else 0.0
    sources_breakdown = {
        src: {
            "count": len(scores),
            "avg_score": (sum(scores) / len(scores)) if scores else 0.0,
        }
        for src, scores in per_source_scores.items()
    }
    db.upsert_annoyance_index(hour_iso, round(avg_index, 2), post_count, sources_breakdown)

    entity_count = 0
    for canonical, entries in entity_rows.items():
        cnt = len(entries)
        avg = sum(s for s, _ in entries) / cnt
        # Most common type
        types = [t for _, t in entries if t]
        entity_type = max(set(types), key=types.count) if types else None
        db.upsert_entity_count(
            entity=canonical,
            entity_type=entity_type,
            hour=hour_iso,
            count=cnt,
            avg_annoyance=round(avg, 2),
        )
        entity_count += 1

    return {
        "hour": hour_iso,
        "posts": post_count,
        "entities": entity_count,
        "index": round(avg_index, 2),
    }


def rebuild_recent() -> list[dict]:
    """Rebuild current hour and previous hour. Called by the aggregator loop."""
    current = db.current_hour_iso()
    prev = db._hour_bump(current, -1)
    results = []
    for h in (prev, current):
        try:
            results.append(rebuild_hour(h))
        except Exception:
            log.exception("aggregator: rebuild failed for %s", h)
    return results
