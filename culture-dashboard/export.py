"""CSV/JSON export of the dashboard's persistent history.

Streams rows from the SQLite cache as CSV without materialising the full
result set in memory. JSON output is build via the same row generators.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from typing import Callable, Iterator

import cache


_EXPORT_TYPES: dict[str, dict] = {
    "surges": {
        "headers": ["source", "key", "alerted_at", "z_score"],
        "query": (
            "SELECT source, key, alerted_at, z_score FROM surge_alerts "
            "WHERE alerted_at >= ? ORDER BY alerted_at DESC"
        ),
    },
    "market_prices": {
        "headers": ["event_slug", "ts", "favorite_question", "favorite_price",
                    "volume", "best_bid", "best_ask", "mid_price", "spread_bps"],
        "query": (
            "SELECT event_slug, ts, favorite_question, favorite_price, volume, "
            "best_bid, best_ask, mid_price, spread_bps FROM market_prices "
            "WHERE ts >= ? ORDER BY ts DESC"
        ),
    },
    "topic_snapshots": {
        "headers": ["ts", "label", "spread", "surge_signal",
                    "sources_json", "sections_json", "market_slugs_json"],
        "query": (
            "SELECT ts, label, spread, surge_signal, sources_json, "
            "sections_json, market_slugs_json FROM topic_snapshots "
            "WHERE ts >= ? ORDER BY ts DESC"
        ),
    },
    "item_history": {
        "headers": ["source", "key", "ts", "score", "velocity"],
        "query": (
            "SELECT source, key, ts, score, velocity FROM item_history "
            "WHERE ts >= ? ORDER BY ts DESC"
        ),
    },
    "index_history": {
        "headers": ["ts", "overall", "sections_json"],
        "query": (
            "SELECT ts, overall, sections_json FROM index_history "
            "WHERE ts >= ? ORDER BY ts DESC"
        ),
    },
    "digests": {
        "headers": ["ts", "model", "body_md", "input_tokens", "output_tokens",
                    "cache_read_tokens", "cache_create_tokens"],
        "query": (
            "SELECT ts, model, body_md, input_tokens, output_tokens, "
            "cache_read_tokens, cache_create_tokens FROM digests "
            "WHERE ts >= ? ORDER BY ts DESC"
        ),
    },
}


def available_types() -> list[str]:
    return list(_EXPORT_TYPES.keys())


def _rows(export_type: str, days: int) -> tuple[list[str], Iterator[tuple]]:
    spec = _EXPORT_TYPES[export_type]
    cutoff = time.time() - days * 86400
    conn = sqlite3.connect(cache._DB_PATH)  # noqa: SLF001 — internal access intentional
    try:
        cur = conn.execute(spec["query"], (cutoff,))
        rows = list(cur.fetchall())
    finally:
        conn.close()
    return list(spec["headers"]), iter(rows)


def stream_csv(export_type: str, days: int) -> Iterator[str]:
    if export_type not in _EXPORT_TYPES:
        raise KeyError(export_type)
    headers, rows = _rows(export_type, days)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    yield _drain(buf)
    for row in rows:
        writer.writerow(row)
        yield _drain(buf)


def as_json(export_type: str, days: int) -> dict:
    if export_type not in _EXPORT_TYPES:
        raise KeyError(export_type)
    headers, rows = _rows(export_type, days)
    return {
        "type": export_type,
        "days": days,
        "headers": headers,
        "rows": [dict(zip(headers, r)) for r in rows],
    }


def _drain(buf: io.StringIO) -> str:
    chunk = buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
    return chunk
