"""Queries extracted from gateway/db.py — sources domain.

Moved out of db.py to keep the connection-pooling/schema module small.
Re-exported back onto db.py at import time, so every existing
``import db; db.<name>`` call site keeps working unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from typing import Optional

import db
from db import _fts_sanitize_query  # noqa: F401 — stays bound; shared helper


def search_sources(q: str, limit: int = 20) -> list[sqlite3.Row]:
    """FTS5 search against source handles, enriched with credibility data."""
    match = _fts_sanitize_query(q)
    if not match:
        return []
    with db.conn() as c:
        try:
            return c.execute(
                """
                SELECT sc.id, sc.source_handle, sc.global_credibility,
                       sc.accuracy_unlocked, sc.total_predictions,
                       sc.correct_predictions, sc.decay_weighted_accuracy,
                       bm25(sources_fts) AS rank
                FROM sources_fts
                JOIN source_credibility sc ON sc.id = sources_fts.rowid
                WHERE sources_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []


def get_source_credibility(source_handle: str) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM source_credibility WHERE source_handle = ?",
            (source_handle,),
        ).fetchone()


def upsert_source_credibility(
    source_handle: str,
    global_credibility: float,
    accuracy_unlocked: bool = False,
    decay_weighted_accuracy: Optional[float] = None,
    total_predictions: int = 0,
    correct_predictions: int = 0,
    categories_active: int = 0,
) -> None:
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            """INSERT INTO source_credibility
                (source_handle, global_credibility, accuracy_unlocked, decay_weighted_accuracy,
                 total_predictions, correct_predictions, categories_active, last_computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_handle) DO UPDATE SET
                global_credibility = excluded.global_credibility,
                accuracy_unlocked = excluded.accuracy_unlocked,
                decay_weighted_accuracy = excluded.decay_weighted_accuracy,
                total_predictions = excluded.total_predictions,
                correct_predictions = excluded.correct_predictions,
                categories_active = excluded.categories_active,
                last_computed_at = excluded.last_computed_at
            """,
            (source_handle, global_credibility, 1 if accuracy_unlocked else 0,
             decay_weighted_accuracy, total_predictions, correct_predictions,
             categories_active, now),
        )
        # Store snapshot
        c.execute(
            "INSERT INTO credibility_snapshots (source_handle, global_credibility, snapshot_at) VALUES (?, ?, ?)",
            (source_handle, global_credibility, now),
        )


def get_category_credibility(source_handle: str, category: str) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM source_category_credibility WHERE source_handle = ? AND category = ?",
            (source_handle, category),
        ).fetchone()


def get_all_category_credibilities(source_handle: str) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM source_category_credibility WHERE source_handle = ? ORDER BY category",
            (source_handle,),
        ).fetchall()


def upsert_category_credibility(
    source_handle: str, category: str, credibility: float,
    prediction_count: int = 0, correct_count: int = 0,
) -> None:
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            """INSERT INTO source_category_credibility
                (source_handle, category, category_credibility, prediction_count, correct_count, last_computed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_handle, category) DO UPDATE SET
                category_credibility = excluded.category_credibility,
                prediction_count = excluded.prediction_count,
                correct_count = excluded.correct_count,
                last_computed_at = excluded.last_computed_at
            """,
            (source_handle, category, credibility, prediction_count, correct_count, now),
        )


def get_credibility_snapshots(source_handle: str, limit: int = 5) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM credibility_snapshots WHERE source_handle = ? ORDER BY snapshot_at DESC LIMIT ?",
            (source_handle, limit),
        ).fetchall()


def list_all_source_credibilities() -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute("SELECT * FROM source_credibility ORDER BY global_credibility DESC").fetchall()


def compute_calibration(source_handle: str) -> Optional[dict]:
    """Compute calibration score for a source (F9).

    Buckets all resolved predictions with a stated probability into 10 bins
    (0-10%, 10-20%, ..., 90-100%). For each bucket, compares the average
    predicted probability to the actual resolution rate.

    Calibration score = 1 - mean(|actual_rate - predicted_avg|) per bucket.
    A perfectly calibrated source scores 1.0.

    Returns None if < 5 calibratable predictions.
    """
    import json as _json

    BUCKETS = [(i / 10, (i + 1) / 10) for i in range(10)]

    with db.conn() as c:
        preds = c.execute(
            "SELECT predicted_probability, resolved_correct FROM predictions "
            "WHERE source_handle = ? AND resolved = 1 "
            "AND predicted_probability IS NOT NULL AND resolved_correct IS NOT NULL",
            (source_handle,),
        ).fetchall()

    if len(preds) < 5:
        return None

    bucket_data = []
    deviations = []
    for low, high in BUCKETS:
        in_bucket = [p for p in preds if low <= (p["predicted_probability"] or 0) < high]
        if not in_bucket:
            continue
        predicted_avg = sum(p["predicted_probability"] for p in in_bucket) / len(in_bucket)
        actual_rate = sum(1 for p in in_bucket if p["resolved_correct"]) / len(in_bucket)
        deviation = abs(actual_rate - predicted_avg)
        deviations.append(deviation)
        bucket_data.append({
            "range": f"{int(low * 100)}-{int(high * 100)}%",
            "predicted": round(predicted_avg, 3),
            "actual": round(actual_rate, 3),
            "count": len(in_bucket),
        })

    if not deviations:
        return None

    score = round(1 - sum(deviations) / len(deviations), 4)
    now = int(time.time())

    with db.conn() as c:
        c.execute(
            """INSERT INTO source_calibration
                (source_handle, calibration_score, calibration_data, total_calibrated, last_computed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_handle) DO UPDATE SET
                calibration_score = excluded.calibration_score,
                calibration_data = excluded.calibration_data,
                total_calibrated = excluded.total_calibrated,
                last_computed_at = excluded.last_computed_at
            """,
            (source_handle, score, _json.dumps({"buckets": bucket_data}), len(preds), now),
        )

    return {"calibration_score": score, "buckets": bucket_data, "total_calibrated": len(preds)}


def get_source_calibration(source_handle: str) -> Optional[dict]:
    """Fetch cached calibration data for a source."""
    import json as _json
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM source_calibration WHERE source_handle = ?",
            (source_handle,),
        ).fetchone()
    if not row:
        return None
    return {
        "calibration_score": row["calibration_score"],
        "buckets": _json.loads(row["calibration_data"] or "{}").get("buckets", []),
        "total_calibrated": row["total_calibrated"],
        "last_computed_at": row["last_computed_at"],
    }


def recompute_all_credibilities() -> int:
    """Recompute all source credibility scores using Bayesian time-decay.

    For each source with at least one resolved prediction:
      1. Exponential time-decay weighting: recent predictions count more.
         weight = exp(-LAMBDA * age_days), half-life ~69 days.
      2. Decay-weighted accuracy: sum(correct_i * weight_i) / sum(weight_i)
      3. Bayesian smoothing toward a 0.5 prior so new sources with few
         predictions don't swing to 0.0 or 1.0.
      4. Per-category breakdown with the same algorithm.
      5. accuracy_unlocked set True when total resolved >= 10.

    Returns the number of sources recomputed.
    """
    import math

    LAMBDA = 0.01       # decay rate: half-life = ln(2)/0.01 ~ 69 days
    PRIOR = 0.5         # Bayesian prior (uninformed)
    STRENGTH = 10       # prior pseudo-count (strength of regression to PRIOR)
    MIN_FOR_UNLOCK = 10 # minimum resolved predictions to unlock accuracy badge
    now = int(time.time())

    # One query for every resolved prediction across every source, instead
    # of 1 + N queries (one per source). For 1 000 sources × 50 resolved
    # each the old shape opened 1 001 connections and issued 1 001 queries;
    # this version issues one and partitions in Python. Ordering by
    # source_handle lets us stream into per-source buckets without building
    # a full dict first, but we build the dict anyway because the per-
    # source block below already loads the whole bucket into memory to
    # compute decay weights.
    with db.conn() as c:
        all_rows = c.execute(
            "SELECT source_handle, resolved_correct, resolved_at, category "
            "FROM predictions "
            "WHERE resolved = 1 AND resolved_correct IS NOT NULL"
        ).fetchall()

    preds_by_handle: dict = {}
    for r in all_rows:
        preds_by_handle.setdefault(r["source_handle"], []).append(r)

    count = 0
    for handle, preds in preds_by_handle.items():
        if not preds:
            continue

        # ── Global decay-weighted accuracy ──────────────────────────────
        weighted_correct = 0.0
        weight_total = 0.0
        total = len(preds)
        correct = 0

        # Per-category accumulators: cat -> {wc, wt, total, correct}
        cat_data: dict = {}

        for p in preds:
            age_days = max(0, (now - (p["resolved_at"] or now)) / 86400)
            decay = math.exp(-LAMBDA * age_days)
            is_correct = 1 if p["resolved_correct"] else 0
            correct += is_correct

            weighted_correct += is_correct * decay
            weight_total += decay

            cat = p["category"] or "other"
            if cat not in cat_data:
                cat_data[cat] = {"wc": 0.0, "wt": 0.0, "total": 0, "correct": 0}
            cd = cat_data[cat]
            cd["wc"] += is_correct * decay
            cd["wt"] += decay
            cd["total"] += 1
            cd["correct"] += is_correct

        dwa = weighted_correct / weight_total if weight_total > 0 else PRIOR
        # Bayesian smoothing: (n * observation + strength * prior) / (n + strength)
        global_cred = (total * dwa + STRENGTH * PRIOR) / (total + STRENGTH)
        unlocked = total >= MIN_FOR_UNLOCK

        upsert_source_credibility(
            source_handle=handle,
            global_credibility=round(global_cred, 6),
            accuracy_unlocked=unlocked,
            decay_weighted_accuracy=round(dwa, 6),
            total_predictions=total,
            correct_predictions=correct,
            categories_active=len(cat_data),
        )

        # ── Per-category scores ─────────────────────────────────────────
        for cat, cd in cat_data.items():
            cat_dwa = cd["wc"] / cd["wt"] if cd["wt"] > 0 else PRIOR
            cat_cred = (cd["total"] * cat_dwa + STRENGTH * PRIOR) / (cd["total"] + STRENGTH)
            upsert_category_credibility(
                source_handle=handle,
                category=cat,
                credibility=round(cat_cred, 6),
                prediction_count=cd["total"],
                correct_count=cd["correct"],
            )

        # Compute calibration alongside credibility (F9).
        try:
            compute_calibration(handle)
        except Exception:
            pass  # calibration is best-effort; don't fail the whole recompute

        count += 1

    return count


def get_source_summary(source_handle: str) -> Optional[sqlite3.Row]:
    if not source_handle:
        return None
    now = int(time.time())
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM source_summaries "
            "WHERE source_handle = ? AND cache_valid_until > ?",
            (source_handle, now),
        ).fetchone()


def upsert_source_summary(source_handle: str, payload: dict) -> int:
    with db.conn() as c:
        c.execute("DELETE FROM source_summaries WHERE source_handle = ?", (source_handle,))
        cur = c.execute(
            """
            INSERT INTO source_summaries (
                source_handle, summary, generated_at, generated_by,
                cache_valid_until, predictions_considered
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                source_handle,
                payload.get("summary") or "",
                int(payload.get("generated_at") or time.time()),
                payload.get("generated_by") or "unknown",
                int(payload.get("cache_valid_until") or (time.time() + 30 * 86400)),
                int(payload.get("predictions_considered") or 0),
            ),
        )
        return cur.lastrowid


def list_stale_source_summaries(limit: int = 50) -> list[sqlite3.Row]:
    now = int(time.time())
    with db.conn() as c:
        return c.execute(
            """
            SELECT sc.source_handle
            FROM source_credibility sc
            LEFT JOIN source_summaries ss ON ss.source_handle = sc.source_handle
            WHERE sc.accuracy_unlocked = 1
              AND (ss.cache_valid_until IS NULL OR ss.cache_valid_until <= ?)
            ORDER BY sc.global_credibility DESC
            LIMIT ?
            """,
            (now, int(limit)),
        ).fetchall()


def get_source_prediction_context(source_handle: str, limit: int = 50) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            """
            SELECT content, category, direction, predicted_probability,
                   resolved, resolved_correct, extracted_at
            FROM predictions
            WHERE source_handle = ?
            ORDER BY extracted_at DESC
            LIMIT ?
            """,
            (source_handle, int(limit)),
        ).fetchall()


__all__ = [
    'search_sources',
    'get_source_credibility',
    'upsert_source_credibility',
    'get_category_credibility',
    'get_all_category_credibilities',
    'upsert_category_credibility',
    'get_credibility_snapshots',
    'list_all_source_credibilities',
    'compute_calibration',
    'get_source_calibration',
    'recompute_all_credibilities',
    'get_source_summary',
    'upsert_source_summary',
    'list_stale_source_summaries',
    'get_source_prediction_context',
]
