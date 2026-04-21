"""Generic TTL cache — the ``ai_cache`` table (migration 050).

Used by ai/extractor.py, ai/categoriser.py, ai/source_summariser.py and
ai/environmental.py for any "call Claude with this prompt, cache the
answer" path that doesn't have its own typed cache table.

Key-space conventions (by feature):
  extract:<sha256(post_text)>            30 d
  categorise:<market_slug>               365 d
  summary:<source_handle>                30 d
  env:<market_slug>                      1 d
  correlation:<signal_id>:<market_slug>  7 d

Values are opaque JSON strings. The caller serialises + deserialises.
``get`` returns ``None`` both for missing keys and for expired rows; the
cleanup path (``purge_expired``) is called out of band.

This module intentionally uses its own sqlite3 connection per call to
avoid contending with the long-lived connection held by db.py / the
rest of the gateway. TTL caches don't care about transactions.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


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


def get(key: str) -> Optional[Any]:
    if not key:
        return None
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT value_json, expires_at FROM ai_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    if row["expires_at"] <= int(time.time()):
        return None
    try:
        return json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        return None


def set(  # noqa: A001 — cache.set is the idiomatic name; shadowing builtin is fine here
    key: str,
    value: Any,
    *,
    ttl_seconds: int,
    feature: str = "unknown",
    model: Optional[str] = None,
) -> None:
    if not key:
        return
    now = int(time.time())
    expires = now + max(1, int(ttl_seconds))
    payload = json.dumps(value)
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO ai_cache (cache_key, value_json, feature, model, created_at, expires_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "  value_json = excluded.value_json, "
                "  feature = excluded.feature, "
                "  model = excluded.model, "
                "  created_at = excluded.created_at, "
                "  expires_at = excluded.expires_at",
                (key, payload, feature, model, now, expires),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # Cache write failures should never break a Claude call.


def delete(key: str) -> None:
    if not key:
        return
    try:
        conn = _connect()
        try:
            conn.execute("DELETE FROM ai_cache WHERE cache_key = ?", (key,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def purge_expired() -> int:
    """Delete expired rows. Safe to call from a cron. Returns rowcount."""
    now = int(time.time())
    try:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM ai_cache WHERE expires_at <= ?", (now,))
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
