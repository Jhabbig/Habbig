"""Queries extracted from gateway/db.py — environmental domain.

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


ENV_VALID_UNITS = frozenset({"co2_mt", "trees", "cars", "homes", "flights"})


def get_environmental_impact(market_id: str) -> Optional[sqlite3.Row]:
    """Return the cached env analysis for *market_id*, or None if absent or
    expired. The caller decides whether to regenerate on a None result —
    this function never calls Claude itself.
    """
    if not market_id:
        return None
    now = int(time.time())
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM environmental_impacts "
            "WHERE market_id = ? AND cache_valid_until > ?",
            (market_id, now),
        ).fetchone()


def get_environmental_impact_any_age(market_id: str) -> Optional[sqlite3.Row]:
    """Return the cached row regardless of TTL — used by the analyser to
    decide whether to regenerate based on price drift.
    """
    if not market_id:
        return None
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM environmental_impacts WHERE market_id = ?",
            (market_id,),
        ).fetchone()


def upsert_environmental_impact(market_id: str, payload: dict) -> int:
    """Atomically replace any existing row for *market_id* with *payload*.

    *payload* must include all schema fields the analyser produces. Missing
    optional fields are persisted as NULL. Returns the row id.
    """
    import json as _json
    sources_json = _json.dumps(payload.get("data_sources") or [])
    with db.conn() as c:
        c.execute("DELETE FROM environmental_impacts WHERE market_id = ?", (market_id,))
        cur = c.execute(
            """
            INSERT INTO environmental_impacts (
                market_id, market_question, market_category,
                generated_at, generated_by, cache_valid_until,
                is_relevant, irrelevance_reason,
                yes_outcome_label, no_outcome_label,
                yes_co2_impact_mt, no_co2_impact_mt,
                yes_impact_description, no_impact_description,
                yes_impact_timeframe, no_impact_timeframe,
                confidence, confidence_reason, data_sources, category,
                yes_market_price_at_gen
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                market_id,
                payload.get("market_question") or "",
                payload.get("market_category"),
                int(payload.get("generated_at") or time.time()),
                payload.get("generated_by") or "unknown",
                int(payload.get("cache_valid_until") or (time.time() + 86400)),
                1 if payload.get("is_relevant") else 0,
                payload.get("irrelevance_reason"),
                payload.get("yes_outcome_label") or "YES",
                payload.get("no_outcome_label") or "NO",
                payload.get("yes_co2_impact_mt"),
                payload.get("no_co2_impact_mt"),
                payload.get("yes_impact_description"),
                payload.get("no_impact_description"),
                payload.get("yes_impact_timeframe"),
                payload.get("no_impact_timeframe"),
                payload.get("confidence"),
                payload.get("confidence_reason"),
                sources_json,
                payload.get("category"),
                payload.get("yes_market_price_at_gen"),
            ),
        )
        return cur.lastrowid


def list_top_environmental_impacts(limit: int = 20) -> list[sqlite3.Row]:
    """Return env-relevant rows ordered by total absolute CO2 impact.

    Used by GET /api/markets/environmental/top and the Intelligence context
    builder. Reads from cache only — never triggers generation. Excludes
    rows with both yes/no impacts NULL (degenerate analyses).
    """
    limit = max(1, min(100, int(limit)))
    with db.conn() as c:
        return c.execute(
            """
            SELECT *,
                   COALESCE(ABS(yes_co2_impact_mt), 0) +
                   COALESCE(ABS(no_co2_impact_mt), 0) AS total_abs_impact
            FROM environmental_impacts
            WHERE is_relevant = 1
              AND (yes_co2_impact_mt IS NOT NULL OR no_co2_impact_mt IS NOT NULL)
            ORDER BY total_abs_impact DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def get_user_env_preferences(user_id: int) -> dict:
    """Return {"show": bool, "unit": str} for *user_id*. New users get the
    schema defaults (show=True, unit='co2_mt') even if their row was created
    before migration 008 ran (the ALTER TABLE default backfills automatically).
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT env_show, env_unit FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"show": True, "unit": "co2_mt"}
    unit = row["env_unit"] if row["env_unit"] in ENV_VALID_UNITS else "co2_mt"
    return {"show": bool(row["env_show"]), "unit": unit}


def set_user_env_preferences(user_id: int, *, show: bool, unit: str) -> bool:
    """Persist environmental display preferences. Validates *unit* against
    ENV_VALID_UNITS — invalid units raise ValueError so callers can return
    a 400 to the client. Returns True if the row was updated.
    """
    if unit not in ENV_VALID_UNITS:
        raise ValueError(f"unit must be one of {sorted(ENV_VALID_UNITS)}")
    with db.conn() as c:
        cur = c.execute(
            "UPDATE users SET env_show = ?, env_unit = ? WHERE id = ?",
            (1 if show else 0, unit, user_id),
        )
    return cur.rowcount > 0


__all__ = [
    'ENV_VALID_UNITS',
    'get_environmental_impact',
    'get_environmental_impact_any_age',
    'upsert_environmental_impact',
    'list_top_environmental_impacts',
    'get_user_env_preferences',
    'set_user_env_preferences',
]
