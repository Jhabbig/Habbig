"""Public AI-feature routes — source summary + admin usage snapshot.

  GET /api/sources/{handle}/summary       plain-English summary (public)
  GET /admin/api/ai/usage                 per-day per-feature rollup (admin)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ai import source_summariser


log = logging.getLogger("ai_routes")


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent / p)
    return Path(__file__).parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


async def source_summary(request: Request, handle: str):
    summary = await source_summariser.generate_source_summary(handle)
    return JSONResponse(summary)


async def admin_ai_usage(request: Request):
    import server
    user = server._require_admin_user(request)
    try:
        days = max(1, min(90, int(request.query_params.get("days") or "14")))
    except ValueError:
        days = 14
    start = int(time.time()) - days * 86400
    conn = _connect()
    try:
        rows = conn.execute(
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
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"claude_usage_log read failed: {exc}")
    finally:
        conn.close()
    return JSONResponse({
        "days": days,
        "rollup": [dict(r) for r in rows],
        "admin": user["email"],
    })


def register(app) -> None:
    app.add_api_route("/api/sources/{handle}/summary", source_summary, methods=["GET"])
    app.add_api_route("/admin/api/ai/usage", admin_ai_usage, methods=["GET"])
