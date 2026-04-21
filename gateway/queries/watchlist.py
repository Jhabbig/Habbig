"""Queries extracted from gateway/db.py — watchlist domain.

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


_ALLOWED_SAVED_ORDER_CLAUSES = frozenset({
    "sp.saved_at DESC",
    "sc.global_credibility DESC NULLS LAST, sp.saved_at DESC",
    "p.resolved_at DESC NULLS LAST, sp.saved_at DESC",
})


def save_prediction(user_id: int, prediction_id: int, notes: Optional[str] = None) -> int:
    """Insert-or-return-existing saved_predictions row. Returns the row id."""
    with db.conn() as c:
        # Ensure the prediction actually exists (FK would catch it, but we
        # prefer a clean 404 at the API layer).
        exists = c.execute("SELECT 1 FROM predictions WHERE id = ?", (prediction_id,)).fetchone()
        if not exists:
            return 0
        try:
            cur = c.execute(
                "INSERT INTO saved_predictions (user_id, prediction_id, saved_at, notes) "
                "VALUES (?, ?, ?, ?)",
                (user_id, prediction_id, int(time.time()), notes),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # Already saved — return the existing id
            row = c.execute(
                "SELECT id FROM saved_predictions WHERE user_id = ? AND prediction_id = ?",
                (user_id, prediction_id),
            ).fetchone()
            return row["id"] if row else 0


def unsave_prediction(user_id: int, prediction_id: int) -> bool:
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM saved_predictions WHERE user_id = ? AND prediction_id = ?",
            (user_id, prediction_id),
        )
        return bool(cur.rowcount)


def is_prediction_saved(user_id: int, prediction_id: int) -> bool:
    with db.conn() as c:
        row = c.execute(
            "SELECT 1 FROM saved_predictions WHERE user_id = ? AND prediction_id = ?",
            (user_id, prediction_id),
        ).fetchone()
        return row is not None


def list_saved_predictions(
    user_id: int,
    resolved_filter: str = "all",  # all | active | correct | incorrect
    sort: str = "saved_at",         # saved_at | credibility | resolution_date
) -> list[sqlite3.Row]:
    where = ["sp.user_id = ?"]
    params: list = [user_id]
    if resolved_filter == "active":
        where.append("p.resolved = 0")
    elif resolved_filter == "correct":
        where.append("p.resolved = 1 AND p.resolved_correct = 1")
    elif resolved_filter == "incorrect":
        where.append("p.resolved = 1 AND p.resolved_correct = 0")
    order = {
        "saved_at": "sp.saved_at DESC",
        "credibility": "sc.global_credibility DESC NULLS LAST, sp.saved_at DESC",
        "resolution_date": "p.resolved_at DESC NULLS LAST, sp.saved_at DESC",
    }.get(sort, "sp.saved_at DESC")
    # Defence-in-depth: assert the chosen ORDER BY clause is one we built
    # ourselves before interpolating into SQL. Without this, a future
    # refactor that accepts a caller-supplied clause would silently
    # become a SQL-injection vector.
    if order not in _ALLOWED_SAVED_ORDER_CLAUSES:
        raise ValueError(f"invalid saved-predictions sort: {order!r}")
    sql = (
        "SELECT sp.id AS saved_id, sp.saved_at, sp.notes, sp.notified_on_resolution, "
        "p.id AS prediction_id, p.content, p.source_handle, p.category, "
        "p.market_id, p.direction, p.predicted_probability, p.source_url, "
        "p.extracted_at, p.resolved, p.resolved_correct, p.resolved_at, "
        "sc.global_credibility, sc.accuracy_unlocked "
        "FROM saved_predictions sp "
        "JOIN predictions p ON p.id = sp.prediction_id "
        "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY {order}"
    )
    with db.conn() as c:
        return c.execute(sql, tuple(params)).fetchall()


def update_saved_prediction_notes(user_id: int, prediction_id: int, notes: Optional[str]) -> bool:
    with db.conn() as c:
        cur = c.execute(
            "UPDATE saved_predictions SET notes = ? WHERE user_id = ? AND prediction_id = ?",
            (notes, user_id, prediction_id),
        )
        return bool(cur.rowcount)


def saved_prediction_ids_for_user(user_id: int) -> set[int]:
    """Return the set of prediction ids saved by this user — small query used
    by the feed to annotate rows with their saved-state without an N+1."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT prediction_id FROM saved_predictions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {r["prediction_id"] for r in rows}


def saved_predictions_pending_resolution_notification(user_id: int) -> list[sqlite3.Row]:
    """Return saved predictions whose underlying prediction just resolved and
    haven't yet been flagged as notified. Notification jobs mark them seen."""
    with db.conn() as c:
        return c.execute(
            "SELECT sp.id AS saved_id, sp.prediction_id, p.resolved_correct, "
            "p.content, p.source_handle "
            "FROM saved_predictions sp "
            "JOIN predictions p ON p.id = sp.prediction_id "
            "WHERE sp.user_id = ? AND p.resolved = 1 AND sp.notified_on_resolution = 0",
            (user_id,),
        ).fetchall()


def mark_saved_prediction_notified(saved_id: int) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE saved_predictions SET notified_on_resolution = 1 WHERE id = ?",
            (saved_id,),
        )


def follow_source(
    user_id: int,
    source_handle: str,
    platform: str = "",
    notify_on_prediction: bool = False,
    notify_min_credibility: float = 0.5,
) -> int:
    source_handle = source_handle.strip()
    if not source_handle:
        return 0
    with db.conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO followed_sources (user_id, source_handle, platform, followed_at, "
                "notify_on_prediction, notify_min_credibility) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, source_handle, platform, int(time.time()),
                 1 if notify_on_prediction else 0, float(notify_min_credibility)),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            row = c.execute(
                "SELECT id FROM followed_sources WHERE user_id = ? AND source_handle = ?",
                (user_id, source_handle),
            ).fetchone()
            return row["id"] if row else 0


def unfollow_source(user_id: int, source_handle: str) -> bool:
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM followed_sources WHERE user_id = ? AND source_handle = ?",
            (user_id, source_handle),
        )
        return bool(cur.rowcount)


def is_following_source(user_id: int, source_handle: str) -> bool:
    with db.conn() as c:
        row = c.execute(
            "SELECT 1 FROM followed_sources WHERE user_id = ? AND source_handle = ?",
            (user_id, source_handle),
        ).fetchone()
        return row is not None


def update_follow_preferences(
    user_id: int,
    source_handle: str,
    notify_on_prediction: bool,
    notify_min_credibility: float,
) -> bool:
    with db.conn() as c:
        cur = c.execute(
            "UPDATE followed_sources SET notify_on_prediction = ?, notify_min_credibility = ? "
            "WHERE user_id = ? AND source_handle = ?",
            (1 if notify_on_prediction else 0, float(notify_min_credibility),
             user_id, source_handle),
        )
        return bool(cur.rowcount)


def list_followed_sources(user_id: int) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT fs.id, fs.source_handle, fs.platform, fs.followed_at, "
            "fs.notify_on_prediction, fs.notify_min_credibility, "
            "sc.global_credibility, sc.accuracy_unlocked, sc.total_predictions "
            "FROM followed_sources fs "
            "LEFT JOIN source_credibility sc ON sc.source_handle = fs.source_handle "
            "WHERE fs.user_id = ? ORDER BY fs.followed_at DESC",
            (user_id,),
        ).fetchall()


def followed_source_handles(user_id: int) -> set[str]:
    """Small query used by feed-ranking code in dashboard backends."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT source_handle FROM followed_sources WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {r["source_handle"] for r in rows}


__all__ = [
    'save_prediction',
    'unsave_prediction',
    'is_prediction_saved',
    'list_saved_predictions',
    'update_saved_prediction_notes',
    'saved_prediction_ids_for_user',
    'saved_predictions_pending_resolution_notification',
    'mark_saved_prediction_notified',
    'follow_source',
    'unfollow_source',
    'is_following_source',
    'update_follow_preferences',
    'list_followed_sources',
    'followed_source_handles',
]
