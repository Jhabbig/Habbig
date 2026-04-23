"""Queries extracted from gateway/db.py — predictions domain.

Moved out of db.py to keep the connection-pooling/schema module small.
Re-exported back onto db.py at import time, so every existing
``import db; db.<name>`` call site keeps working unchanged.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

import db
from db import _fts_sanitize_query  # noqa: F401 — stays bound; shared helper


def search_predictions(q: str, limit: int = 20) -> list[sqlite3.Row]:
    """FTS5 search against predictions. Joins source_credibility for scoring.

    Results come back with a ``highlight`` column containing the matched
    snippet wrapped in <mark>…</mark> tags (safe to render as HTML because
    FTS5 snippet() escapes non-tag characters itself, but the CALLER MUST
    still html-escape the caller-provided base text).
    """
    match = _fts_sanitize_query(q)
    if not match:
        return []
    with db.conn() as c:
        try:
            return c.execute(
                """
                SELECT p.id, p.content, p.source_handle, p.category,
                       p.market_id, p.direction, p.predicted_probability,
                       p.extracted_at, p.resolved, p.resolved_correct,
                       sc.global_credibility, sc.accuracy_unlocked,
                       snippet(predictions_fts, 0, '<mark>', '</mark>', '…', 16) AS highlight,
                       bm25(predictions_fts) AS rank
                FROM predictions_fts
                JOIN predictions p ON p.id = predictions_fts.rowid
                LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle
                WHERE predictions_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Malformed MATCH — return empty rather than 500.
            return []


def create_prediction(
    source_handle: str, content: str, category: str = "other",
    market_id: Optional[str] = None, direction: Optional[str] = None,
    predicted_probability: Optional[float] = None, source_url: Optional[str] = None,
) -> int:
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO predictions (source_handle, market_id, category, direction, "
            "predicted_probability, content, source_url, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source_handle, market_id, category, direction,
             predicted_probability, content, source_url, now),
        )
        return cur.lastrowid


def get_unresolved_market_ids() -> list[str]:
    """Return distinct market_ids that have unresolved predictions."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT DISTINCT market_id FROM predictions "
            "WHERE resolved = 0 AND market_id IS NOT NULL AND market_id != ''"
        ).fetchall()
    return [r["market_id"] for r in rows]


def resolve_predictions_for_market(market_id: str, outcome_yes: bool) -> int:
    """Mark all unresolved predictions for *market_id* as resolved.

    direction == "YES" → resolved_correct = 1 if outcome_yes, else 0
    direction == "NO"  → resolved_correct = 0 if outcome_yes, else 1
    direction == NULL or other → resolved_correct = NULL (unknown)

    Returns the number of rows updated.
    """
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE predictions SET resolved = 1, resolved_at = ?, "
            "resolved_correct = CASE "
            "  WHEN direction = 'YES' THEN ? "
            "  WHEN direction = 'NO'  THEN ? "
            "  ELSE NULL END "
            "WHERE market_id = ? AND resolved = 0",
            (now, 1 if outcome_yes else 0, 0 if outcome_yes else 1, market_id),
        )
        return c.execute("SELECT changes()").fetchone()[0]


def get_predictions_for_market(market_id: str) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT p.*, sc.global_credibility, sc.accuracy_unlocked, sc.decay_weighted_accuracy, "
            "scc.category_credibility "
            "FROM predictions p "
            "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
            "LEFT JOIN source_category_credibility scc ON scc.source_handle = p.source_handle AND scc.category = p.category "
            "WHERE p.market_id = ? ORDER BY p.extracted_at DESC",
            (market_id,),
        ).fetchall()


def list_recent_predictions(limit: int = 50, category: Optional[str] = None) -> list[sqlite3.Row]:
    with db.conn() as c:
        if category:
            return c.execute(
                "SELECT p.*, sc.global_credibility, sc.accuracy_unlocked "
                "FROM predictions p "
                "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
                "WHERE p.category = ? ORDER BY p.extracted_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        return c.execute(
            "SELECT p.*, sc.global_credibility, sc.accuracy_unlocked "
            "FROM predictions p "
            "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
            "ORDER BY p.extracted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def calculate_betyc_probability(predictions: list) -> dict:
    """
    Credibility-weighted average of predicted probabilities.

    For predictions with explicit probability stated by source:
      use that probability, weighted by source category_credibility

    For YES/NO directional predictions without explicit %:
      YES from source with credibility X -> probability = 0.5 + (X - 0.5) * 0.8
      NO from source with credibility X -> probability = 0.5 - (X - 0.5) * 0.8

    Final result clamped to [0.05, 0.95]
    """
    if not predictions:
        return {
            "betyc_yes_probability": None,
            "betyc_no_probability": None,
            "betyc_edge": None,
            "betyc_source_count": 0,
            "betyc_confidence": "Insufficient data",
        }

    weighted_sum = 0.0
    weight_total = 0.0
    qualifying_sources = 0
    accuracy_unlocked_count = 0
    cred_sum = 0.0

    for p in predictions:
        cred = p.get("category_credibility") or p.get("global_credibility") or 0.5
        prob = p.get("predicted_probability")

        if prob is not None:
            weighted_sum += prob * cred
            weight_total += cred
        else:
            direction = (p.get("direction") or "").upper()
            if direction == "YES":
                inferred = 0.5 + (cred - 0.5) * 0.8
            elif direction == "NO":
                inferred = 0.5 - (cred - 0.5) * 0.8
            else:
                continue
            weighted_sum += inferred * cred
            weight_total += cred

        qualifying_sources += 1
        cred_sum += cred
        if p.get("accuracy_unlocked"):
            accuracy_unlocked_count += 1

    if weight_total == 0 or qualifying_sources == 0:
        return {
            "betyc_yes_probability": None,
            "betyc_no_probability": None,
            "betyc_edge": None,
            "betyc_source_count": 0,
            "betyc_confidence": "Insufficient data",
        }

    raw_prob = weighted_sum / weight_total
    clamped = max(0.05, min(0.95, raw_prob))
    avg_cred = cred_sum / qualifying_sources

    # Confidence levels
    if qualifying_sources >= 5 and avg_cred >= 0.6 and accuracy_unlocked_count > qualifying_sources / 2:
        confidence = "High"
    elif qualifying_sources >= 3 or 0.4 <= avg_cred <= 0.6:
        confidence = "Medium"
    elif qualifying_sources >= 1:
        confidence = "Low"
    else:
        confidence = "Insufficient data"

    return {
        "betyc_yes_probability": round(clamped, 4),
        "betyc_no_probability": round(1 - clamped, 4),
        "betyc_edge": None,  # Caller sets this based on market price
        "betyc_source_count": qualifying_sources,
        "betyc_confidence": confidence,
    }


def get_prediction_extraction(post_hash: str) -> Optional[sqlite3.Row]:
    if not post_hash:
        return None
    now = int(time.time())
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM prediction_extractions "
            "WHERE post_hash = ? AND cache_valid_until > ?",
            (post_hash, now),
        ).fetchone()


def upsert_prediction_extraction(post_hash: str, payload: dict) -> int:
    import json as _json
    with db.conn() as c:
        c.execute("DELETE FROM prediction_extractions WHERE post_hash = ?", (post_hash,))
        cur = c.execute(
            """
            INSERT INTO prediction_extractions (
                post_hash, schema_version, source_post_id, source_handle,
                generated_at, generated_by, cache_valid_until,
                is_prediction, claim, direction, explicit_probability,
                implicit_confidence, time_frame, category,
                contains_sarcasm, is_conditional, raw_payload
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                post_hash,
                int(payload.get("schema_version", 1)),
                payload.get("source_post_id"),
                payload.get("source_handle"),
                int(payload.get("generated_at") or time.time()),
                payload.get("generated_by") or "unknown",
                int(payload.get("cache_valid_until") or (time.time() + 30 * 86400)),
                1 if payload.get("is_prediction") else 0,
                payload.get("claim"),
                payload.get("direction"),
                payload.get("explicit_probability"),
                payload.get("implicit_confidence"),
                payload.get("time_frame"),
                payload.get("category"),
                1 if payload.get("contains_sarcasm") else 0,
                1 if payload.get("is_conditional") else 0,
                _json.dumps(payload.get("raw_payload") or {}),
            ),
        )
        return cur.lastrowid


def insert_reextracted_prediction(payload: dict) -> int:
    with db.conn() as c:
        cur = c.execute(
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
                payload.get("original_prediction_id"),
                payload.get("source_handle"),
                payload.get("market_id"),
                payload.get("category"),
                payload.get("direction"),
                payload.get("predicted_probability"),
                payload.get("content") or "",
                payload.get("source_url"),
                int(payload.get("extracted_at") or time.time()),
                payload.get("claim"),
                payload.get("explicit_probability"),
                payload.get("implicit_confidence"),
                payload.get("time_frame"),
                1 if payload.get("contains_sarcasm") else 0,
                1 if payload.get("is_conditional") else 0,
                1 if payload.get("matches_original") else 0,
                payload.get("diff_summary"),
            ),
        )
        return cur.lastrowid


def reextraction_diff_summary() -> dict:
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN matches_original = 1 THEN 1 ELSE 0 END) AS matches "
            "FROM predictions_reextracted"
        ).fetchone()
    total = int(row["total"] or 0) if row else 0
    matches = int(row["matches"] or 0) if row else 0
    return {
        "total": total,
        "matches": matches,
        "diffs": total - matches,
        "match_rate": round(matches / total, 4) if total else 0.0,
    }


def apply_reextraction_switchover() -> dict:
    updated = 0
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM predictions_reextracted WHERE original_prediction_id IS NOT NULL"
        ).fetchall()
        for r in rows:
            c.execute(
                "UPDATE predictions SET category = ?, direction = ?, "
                "predicted_probability = ? WHERE id = ?",
                (r["category"], r["direction"],
                 r["predicted_probability"], r["original_prediction_id"]),
            )
            updated += 1
        c.execute("DELETE FROM predictions_reextracted")
    return {"updated": updated}


def create_user_prediction(
    *,
    user_id: int,
    market_slug: str,
    market_question: str,
    category: str,
    predicted_outcome: str,
    predicted_probability: float,
    reasoning: Optional[str] = None,
    market_price_at_prediction: Optional[float] = None,
    is_public: bool = False,
    is_anonymous: bool = False,
) -> int:
    """Insert a user's prediction on an active market. Raises on UNIQUE
    violation (user already has an unresolved prediction on this market).

    Caller should compute edge_at_prediction once market_price is known.
    """
    edge = None
    if market_price_at_prediction is not None:
        edge = abs(predicted_probability - market_price_at_prediction)
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO user_predictions "
            "(user_id, market_slug, market_question, category, "
            " predicted_outcome, predicted_probability, reasoning, "
            " market_price_at_prediction, edge_at_prediction, "
            " is_public, is_anonymous, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, market_slug, market_question, category,
                predicted_outcome, float(predicted_probability), reasoning,
                market_price_at_prediction, edge,
                1 if is_public else 0,
                1 if is_anonymous else 0,
                int(time.time()),
            ),
        )
        return cur.lastrowid


def get_user_prediction(prediction_id: int):
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM user_predictions WHERE id = ?",
            (prediction_id,),
        ).fetchone()


def get_active_user_prediction(user_id: int, market_slug: str):
    """Return the user's unresolved prediction on a market, or None."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM user_predictions "
            "WHERE user_id = ? AND market_slug = ? AND resolved = 0",
            (user_id, market_slug),
        ).fetchone()


def update_user_prediction(
    prediction_id: int,
    *,
    predicted_probability: Optional[float] = None,
    reasoning: Optional[str] = None,
    is_public: Optional[bool] = None,
) -> bool:
    """Edit a user's own unresolved prediction.

    Direction (predicted_outcome) is deliberately NOT editable — once a
    user has committed to YES vs NO, the choice is final. Only probability,
    reasoning, and public-visibility can be updated. Caller must enforce
    the 24h edit window.
    """
    fields = []
    params = []
    if predicted_probability is not None:
        fields.append("predicted_probability = ?"); params.append(float(predicted_probability))
    if reasoning is not None:
        fields.append("reasoning = ?"); params.append(reasoning)
    if is_public is not None:
        fields.append("is_public = ?"); params.append(1 if is_public else 0)
    if not fields:
        return False
    params.append(prediction_id)
    with db.conn() as c:
        cur = c.execute(
            f"UPDATE user_predictions SET {', '.join(fields)} "
            "WHERE id = ? AND resolved = 0",
            tuple(params),
        )
        return cur.rowcount > 0


def list_user_predictions(
    user_id: int,
    *,
    resolved: Optional[bool] = None,
    limit: int = 100,
    offset: int = 0,
):
    """List one user's predictions, newest first.

    If resolved is None, returns both active and resolved predictions —
    which is what the /predictions history page wants. Filtered variants
    are for the 'active only' or 'resolved only' tabs.
    """
    where = ["user_id = ?"]
    params: list = [user_id]
    if resolved is not None:
        where.append("resolved = ?")
        params.append(1 if resolved else 0)
    params.extend([limit, offset])
    with db.conn() as c:
        return c.execute(
            f"SELECT * FROM user_predictions WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()


def list_public_user_predictions(user_id: int, limit: int = 100):
    """Only is_public=1 rows, for the /predictions/public/{user_id} profile."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM user_predictions "
            "WHERE user_id = ? AND is_public = 1 "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def get_user_prediction_stats(user_id: int):
    """Return the user's cached stats row. None if never computed."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM user_prediction_stats WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def upsert_user_prediction_stats(
    user_id: int,
    *,
    total: int,
    resolved: int,
    correct: int,
    accuracy: Optional[float],
    avg_brier: Optional[float],
    avg_timing: Optional[float],
    current_streak: int = 0,
    best_streak: int = 0,
) -> None:
    """Recompute-and-store the user's cached stats row. Called from the
    resolution job after each batch of newly-resolved predictions."""
    with db.conn() as c:
        c.execute(
            "INSERT INTO user_prediction_stats "
            "(user_id, total_predictions, resolved_predictions, "
            " correct_predictions, accuracy, avg_brier_score, "
            " avg_timing_score, current_streak, best_streak) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  total_predictions = excluded.total_predictions, "
            "  resolved_predictions = excluded.resolved_predictions, "
            "  correct_predictions = excluded.correct_predictions, "
            "  accuracy = excluded.accuracy, "
            "  avg_brier_score = excluded.avg_brier_score, "
            "  avg_timing_score = excluded.avg_timing_score, "
            "  current_streak = excluded.current_streak, "
            "  best_streak = excluded.best_streak",
            (user_id, total, resolved, correct, accuracy,
             avg_brier, avg_timing, current_streak, best_streak),
        )


__all__ = [
    'search_predictions',
    'create_prediction',
    'get_unresolved_market_ids',
    'resolve_predictions_for_market',
    'get_predictions_for_market',
    'list_recent_predictions',
    'calculate_betyc_probability',
    'get_prediction_extraction',
    'upsert_prediction_extraction',
    'insert_reextracted_prediction',
    'reextraction_diff_summary',
    'apply_reextraction_switchover',
    'create_user_prediction',
    'get_user_prediction',
    'get_active_user_prediction',
    'update_user_prediction',
    'list_user_predictions',
    'list_public_user_predictions',
    'get_user_prediction_stats',
    'upsert_user_prediction_stats',
]
