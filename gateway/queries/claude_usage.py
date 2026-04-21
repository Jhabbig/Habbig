"""Queries extracted from gateway/db.py — claude_usage domain.

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


CLAUDE_FEATURES = frozenset({
    "extraction",
    "categorisation",
    "summarisation",
    "intelligence_chat",
    "environmental",
    "retrospective",
})


def log_claude_usage(
    *,
    feature: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    cached_hit: bool = False,
) -> int:
    """Append one row to claude_usage_log. Never raises."""
    try:
        with db.conn() as c:
            cur = c.execute(
                "INSERT INTO claude_usage_log "
                "(timestamp, feature, model, input_tokens, output_tokens, cost_usd, cached_hit) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    int(time.time()), feature, model,
                    int(input_tokens or 0), int(output_tokens or 0),
                    float(cost_usd or 0.0),
                    1 if cached_hit else 0,
                ),
            )
            return cur.lastrowid
    except Exception:
        return 0


def claude_usage_between(start_ts: int, end_ts: int) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM claude_usage_log "
            "WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp DESC",
            (int(start_ts), int(end_ts)),
        ).fetchall()


def claude_usage_daily_rollup(days: int = 7) -> list[dict]:
    days = max(1, min(90, int(days)))
    now = int(time.time())
    start = now - days * 86400
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT
                strftime('%Y-%m-%d', timestamp, 'unixepoch') AS day,
                feature,
                COUNT(*) AS calls,
                SUM(cached_hit) AS cache_hits,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(cost_usd) AS cost_usd
            FROM claude_usage_log
            WHERE timestamp >= ?
            GROUP BY day, feature
            ORDER BY day DESC, feature ASC
            """,
            (start,),
        ).fetchall()
    return [
        {
            "day": r["day"],
            "feature": r["feature"],
            "calls": int(r["calls"] or 0),
            "cache_hits": int(r["cache_hits"] or 0),
            "input_tokens": int(r["input_tokens"] or 0),
            "output_tokens": int(r["output_tokens"] or 0),
            "cost_usd": float(r["cost_usd"] or 0.0),
        }
        for r in rows
    ]


def claude_usage_day_total(day_utc: str) -> dict:
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT feature, COUNT(*) AS calls,
                   SUM(cached_hit) AS cache_hits,
                   SUM(cost_usd) AS cost_usd
            FROM claude_usage_log
            WHERE strftime('%Y-%m-%d', timestamp, 'unixepoch') = ?
            GROUP BY feature
            """,
            (day_utc,),
        ).fetchall()
    by_feature = {
        r["feature"]: {
            "calls": int(r["calls"] or 0),
            "cache_hits": int(r["cache_hits"] or 0),
            "cost_usd": float(r["cost_usd"] or 0.0),
        }
        for r in rows
    }
    return {
        "day": day_utc,
        "calls": sum(f["calls"] for f in by_feature.values()),
        "cost_usd": round(sum(f["cost_usd"] for f in by_feature.values()), 4),
        "by_feature": by_feature,
    }


__all__ = [
    'CLAUDE_FEATURES',
    'log_claude_usage',
    'claude_usage_between',
    'claude_usage_daily_rollup',
    'claude_usage_day_total',
]
