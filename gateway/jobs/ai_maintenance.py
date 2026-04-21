"""Periodic housekeeping for the ai/ layer.

Three registered jobs:

  purge_expired_ai_cache
    Daily (03:42 UTC). Deletes rows from ai_cache whose expires_at has
    passed. Modest table but worth keeping trimmed so the hot-path
    SELECTs remain O(log n).

  recompute_calibration_scores
    Every 6 hours. Reads all source_prediction_records with a stated
    probability + resolution, feeds them through credibility.calibration
    in memory (no Claude), and writes per-source calibration_score +
    sample_size + unlocked flag back onto the sources table.

  backfill_reextraction
    Runs the Claude-backed extractor over predictions with
    source_url starting "post:" (our extractor's convention) where
    ai_cache has no matching row. Cheap — just fills the cache so
    subsequent calls skip Claude.

All three are tolerant of missing tables and swallow exceptions into
log lines; they must never leave a permanent lock on the DB.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.ai_maintenance")


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent.parent / p)
    return Path(__file__).parent.parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


# ── ai_cache purge ──────────────────────────────────────────────────────────


@register_job("purge_expired_ai_cache")
async def purge_expired_ai_cache() -> dict[str, Any]:
    from ai import cache
    removed = cache.purge_expired()
    log.info("ai_cache: purged %d expired rows", removed)
    return {"removed": removed}


register_cron("purge_expired_ai_cache", hour=3, minute=42)


# ── Calibration recompute ───────────────────────────────────────────────────


@register_job("recompute_calibration_scores")
async def recompute_calibration_scores() -> dict[str, Any]:
    from credibility.calibration import compute_brier_score

    conn = _connect()
    try:
        if not _table_exists(conn, "source_prediction_records"):
            return {"skipped": "source_prediction_records missing"}
        if not _table_exists(conn, "sources"):
            return {"skipped": "sources missing"}

        # Identify sources that have scoreable records.
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(source_prediction_records)")}
        if "predicted_probability_stated" not in cols or "resolved_correct" not in cols:
            return {"skipped": "calibration columns not present"}

        handles = [r["source_handle"] for r in conn.execute(
            "SELECT DISTINCT source_handle FROM source_prediction_records "
            "WHERE predicted_probability_stated IS NOT NULL "
            "AND resolved_correct IS NOT NULL"
        ).fetchall()]

        updated = 0
        unlocked = 0
        for handle in handles:
            recs = [dict(r) for r in conn.execute(
                "SELECT predicted_probability_stated, resolved_correct "
                "FROM source_prediction_records "
                "WHERE source_handle = ? "
                "AND predicted_probability_stated IS NOT NULL "
                "AND resolved_correct IS NOT NULL",
                (handle,),
            ).fetchall()]
            result = compute_brier_score(recs)
            source_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
            calib_col = "calibration_score" if "calibration_score" in source_cols else None
            sample_col = "calibration_sample_size" if "calibration_sample_size" in source_cols else None
            unlocked_col = "calibration_unlocked" if "calibration_unlocked" in source_cols else None
            if calib_col is None:
                continue
            fields = []
            args: list[Any] = []
            if result is None:
                fields.append(f"{calib_col} = NULL")
                if sample_col:
                    fields.append(f"{sample_col} = 0")
                if unlocked_col:
                    fields.append(f"{unlocked_col} = 0")
            else:
                fields.append(f"{calib_col} = ?"); args.append(result["calibration"])
                if sample_col:
                    fields.append(f"{sample_col} = ?"); args.append(result["sample_size"])
                if unlocked_col:
                    fields.append(f"{unlocked_col} = 1")
                    unlocked += 1
                updated += 1
            args.append(handle)
            # 'sources' schema varies across branches — try handle first, fall
            # back to source_handle if present.
            if "handle" in source_cols:
                conn.execute(
                    f"UPDATE sources SET {', '.join(fields)} WHERE handle = ?",
                    tuple(args),
                )
            elif "source_handle" in source_cols:
                conn.execute(
                    f"UPDATE sources SET {', '.join(fields)} WHERE source_handle = ?",
                    tuple(args),
                )
        conn.commit()
        return {
            "sources_examined": len(handles),
            "calibrated": updated,
            "unlocked": unlocked,
        }
    except sqlite3.Error as exc:
        log.warning("calibration recompute failed: %s", exc)
        return {"error": str(exc)}
    finally:
        conn.close()


for _h in (0, 6, 12, 18):
    register_cron("recompute_calibration_scores", hour=_h, minute=25)


# ── Re-extraction backfill ──────────────────────────────────────────────────


@register_job("reextract_predictions_backfill")
async def reextract_predictions_backfill(limit: int = 50) -> dict[str, Any]:
    """Re-run the Claude extractor over predictions whose original text
    isn't in ai_cache yet. Updates category/direction/probability from
    the new parse if the cached extraction agrees with the DB row.

    Does NOT overwrite when the new extraction disagrees — flags for
    human review via insert into predictions_reextracted (if present).
    """
    from ai import extractor

    conn = _connect()
    try:
        if not _table_exists(conn, "predictions"):
            return {"skipped": "no predictions table"}

        rows = conn.execute(
            "SELECT id, source_handle, content, category, direction, "
            "predicted_probability, source_url "
            "FROM predictions "
            "WHERE content IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()

    processed = 0
    cache_fills = 0
    staged_diffs = 0
    for row in rows:
        key = f"extract:{hashlib.sha256((row['content'] or '').encode('utf-8')).hexdigest()}"
        from ai import cache as _cache
        if _cache.get(key) is not None:
            continue
        # Fresh Claude call. Respect the extractor's own TTL + cache write.
        preds = await extractor.extract_predictions_from_post(
            row["content"], post_id=str(row["id"]),
        )
        processed += 1
        cache_fills += 1
        if not preds:
            continue
        primary = preds[0]
        new_direction = (primary.get("direction") or "").upper() or None
        new_category = primary.get("category") or row["category"]
        new_prob = primary.get("explicit_probability") or row["predicted_probability"]
        matches = (
            new_direction == row["direction"]
            and new_category == row["category"]
            and (new_prob or 0) == (row["predicted_probability"] or 0)
        )
        if not matches:
            staged_diffs += 1
            _stage_diff(row["id"], row, primary)

    return {
        "processed": processed,
        "cache_fills": cache_fills,
        "staged_diffs": staged_diffs,
    }


def _stage_diff(original_id: int, row: sqlite3.Row, new: dict) -> None:
    conn = _connect()
    try:
        if not _table_exists(conn, "predictions_reextracted"):
            return
        diff_parts: list[str] = []
        if (new.get("direction") or "").upper() != row["direction"]:
            diff_parts.append(f"direction: {row['direction']} → {new.get('direction')}")
        if new.get("category") != row["category"]:
            diff_parts.append(f"category: {row['category']} → {new.get('category')}")
        if (new.get("explicit_probability") or 0) != (row["predicted_probability"] or 0):
            diff_parts.append(
                f"probability: {row['predicted_probability']} → {new.get('explicit_probability')}"
            )
        conn.execute(
            """
            INSERT INTO predictions_reextracted (
                original_prediction_id, source_handle, market_id, category,
                direction, predicted_probability, content, source_url,
                extracted_at, claim, explicit_probability, implicit_confidence,
                time_frame, contains_sarcasm, is_conditional,
                matches_original, diff_summary
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                original_id,
                row["source_handle"],
                None,
                new.get("category"),
                (new.get("direction") or "").upper() or None,
                new.get("explicit_probability"),
                row["content"],
                row["source_url"],
                int(time.time()),
                new.get("claim"),
                new.get("explicit_probability"),
                new.get("implicit_confidence"),
                new.get("time_frame"),
                1 if new.get("contains_sarcasm") else 0,
                1 if new.get("is_conditional") else 0,
                0,
                "; ".join(diff_parts) if diff_parts else None,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        log.warning("stage diff for id=%s failed: %s", original_id, exc)
    finally:
        conn.close()


# Backfill runs nightly — a single chunk of 50 per run keeps spend
# predictable even if the predictions table grows to tens of thousands.
register_cron("reextract_predictions_backfill", hour=4, minute=13)
