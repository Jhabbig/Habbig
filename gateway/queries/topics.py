"""Queries extracted from gateway/db.py — topics domain.

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


def create_topic(user_id: int, name: str, keywords: list[str], schedule_minutes: int = 60) -> int:
    import json as _json
    now = int(time.time())
    next_pull = now + schedule_minutes * 60
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO user_topics (user_id, name, keywords, schedule_minutes, next_pull_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name, _json.dumps(keywords), schedule_minutes, next_pull, now),
        )
        return cur.lastrowid


def list_topics(user_id: int) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM user_topics WHERE user_id = ? AND is_active = 1 ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()


def get_topic(topic_id: int) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute("SELECT * FROM user_topics WHERE id = ?", (topic_id,)).fetchone()


def delete_topic(topic_id: int) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM user_topic_analyses WHERE user_topic_id = ?", (topic_id,))
        c.execute("DELETE FROM user_topic_predictions WHERE user_topic_id = ?", (topic_id,))
        c.execute("DELETE FROM user_topics WHERE id = ?", (topic_id,))


def count_user_topics(user_id: int) -> int:
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM user_topics WHERE user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
        return row[0] if row else 0


def update_topic_pull(topic_id: int, posts_found: int = 0, predictions_extracted: int = 0) -> None:
    now = int(time.time())
    topic = get_topic(topic_id)
    if not topic:
        return
    schedule = topic["schedule_minutes"] or 60
    with db.conn() as c:
        c.execute(
            "UPDATE user_topics SET last_pulled_at = ?, next_pull_at = ?, "
            "posts_found_total = posts_found_total + ?, predictions_extracted_total = predictions_extracted_total + ? "
            "WHERE id = ?",
            (now, now + schedule * 60, posts_found, predictions_extracted, topic_id),
        )


def get_due_topics() -> list[sqlite3.Row]:
    """Get topics that are due for a pull (next_pull_at <= now)."""
    now = int(time.time())
    with db.conn() as c:
        return c.execute(
            "SELECT t.*, u.email FROM user_topics t "
            "JOIN users u ON u.id = t.user_id "
            "WHERE t.is_active = 1 AND t.next_pull_at <= ?",
            (now,),
        ).fetchall()


def add_topic_prediction(topic_id: int, prediction_id: int) -> None:
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO user_topic_predictions (user_topic_id, prediction_id, pulled_at) VALUES (?, ?, ?)",
            (topic_id, prediction_id, now),
        )


def get_topic_predictions(topic_id: int, limit: int = 50) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT p.*, sc.global_credibility, sc.accuracy_unlocked, "
            "scc.category_credibility "
            "FROM user_topic_predictions tp "
            "JOIN predictions p ON p.id = tp.prediction_id "
            "LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
            "LEFT JOIN source_category_credibility scc ON scc.source_handle = p.source_handle AND scc.category = p.category "
            "WHERE tp.user_topic_id = ? ORDER BY tp.pulled_at DESC LIMIT ?",
            (topic_id, limit),
        ).fetchall()


def save_topic_analysis(
    topic_id: int, signal_direction: str, summary: str,
    top_signals: list, contradictions: list, relevant_markets: list,
    confidence: str, confidence_reason: str,
) -> int:
    import json as _json
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO user_topic_analyses "
            "(user_topic_id, signal_direction, summary, top_signals, contradictions, "
            "relevant_markets, confidence, confidence_reason, generated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (topic_id, signal_direction, summary, _json.dumps(top_signals),
             _json.dumps(contradictions), _json.dumps(relevant_markets),
             confidence, confidence_reason, now),
        )
        return cur.lastrowid


def get_latest_topic_analysis(topic_id: int) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM user_topic_analyses WHERE user_topic_id = ? ORDER BY generated_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()


__all__ = [
    'create_topic',
    'list_topics',
    'get_topic',
    'delete_topic',
    'count_user_topics',
    'update_topic_pull',
    'get_due_topics',
    'add_topic_prediction',
    'get_topic_predictions',
    'save_topic_analysis',
    'get_latest_topic_analysis',
]
