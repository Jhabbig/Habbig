"""Admin /admin/logs/* — in-memory ring-buffer log viewer.

Registered by being imported at the bottom of ``server.py`` (same pattern
as ``admin_jobs_routes``, ``admin_emails_routes``, etc.).

Routes exposed:
    GET  /admin/logs/live       Most recent structured log records
    GET  /admin/logs/errors     ERROR-level records grouped by (logger, message)
    GET  /admin/logs/search     Free-text substring search over the ring buffer

All three read from the in-memory ring buffer populated by
``logging_config.configure_logging()`` so queries are cheap and do not
hit disk. Every route goes through ``server._require_admin_user``.

Extracted from server.py 2026-05-16 to close audit #24 MED #1
(server.py LOC creep).
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

import server
from logging_config import (
    ring_buffer as _log_ring_buffer,
    is_logtail_configured,
    SERVICE_NAME as _LOG_SERVICE_NAME,
)

log = logging.getLogger("admin_logs")


def _parse_log_query(request: Request) -> dict:
    """Extract common log-filter params from query string."""
    try:
        limit = int(request.query_params.get("limit", "50") or 50)
    except ValueError:
        limit = 50
    return {
        "level": (request.query_params.get("level") or "").upper() or None,
        "service": request.query_params.get("service") or None,
        "q": request.query_params.get("q") or None,
        "limit": max(1, min(limit, 500)),
    }


@server.app.get("/admin/logs/live")
async def admin_logs_live(request: Request):
    """Return the most recent structured log records from the ring buffer.

    Query params:
      level   INFO|WARNING|ERROR — minimum level (default: all)
      service app|scraper|worker|all — filter by service name
      q       substring search inside the JSON payload
      limit   1-500 (default 50)
    """
    admin = server._require_admin_user(request)
    if server._is_rate_limited(f"admin_logs_live:{admin['email']}", 120, 60):
        return JSONResponse(
            {"error": "Log tail polled too frequently."},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    params = _parse_log_query(request)
    records = _log_ring_buffer.snapshot(
        level=params["level"],
        service=params["service"],
        contains=params["q"],
        limit=params["limit"],
    )
    return JSONResponse({
        "records": records,
        "count": len(records),
        "capacity": _log_ring_buffer.capacity,
        "logtail_configured": is_logtail_configured(),
        "service": _LOG_SERVICE_NAME,
    })


@server.app.get("/admin/logs/errors")
async def admin_logs_errors(request: Request):
    """Return ERROR-level records grouped by (logger, message)."""
    admin = server._require_admin_user(request)
    if server._is_rate_limited(f"admin_logs_errors:{admin['email']}", 60, 60):
        return JSONResponse(
            {"error": "Error log polled too frequently."},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    records = _log_ring_buffer.snapshot(level="ERROR", limit=500)

    grouped: dict = {}
    for rec in records:
        logger_name = rec.get("logger", "unknown")
        msg = (rec.get("message") or "")[:200]
        key = (logger_name, msg)
        if key not in grouped:
            grouped[key] = {
                "logger": logger_name,
                "message": msg,
                "service": rec.get("service"),
                "count": 0,
                "first_seen": rec.get("timestamp"),
                "last_seen": rec.get("timestamp"),
                "sample": rec,
            }
        g = grouped[key]
        g["count"] += 1
        ts = rec.get("timestamp")
        if ts:
            if not g["first_seen"] or ts < g["first_seen"]:
                g["first_seen"] = ts
            if not g["last_seen"] or ts > g["last_seen"]:
                g["last_seen"] = ts

    groups = sorted(grouped.values(),
                    key=lambda g: g["last_seen"] or "",
                    reverse=True)
    return JSONResponse({
        "groups": groups,
        "total_errors": sum(g["count"] for g in groups),
        "distinct_errors": len(groups),
    })


@server.app.get("/admin/logs/search")
async def admin_logs_search(request: Request):
    """Free-text substring search over the ring buffer.

    For richer queries (regex, multi-day retention) use BetterStack directly.
    """
    admin = server._require_admin_user(request)
    if server._is_rate_limited(f"admin_logs_search:{admin['email']}", 30, 60):
        return JSONResponse(
            {"error": "Log search rate limit reached."},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    params = _parse_log_query(request)
    try:
        limit = int(request.query_params.get("limit", "100") or 100)
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))
    records = _log_ring_buffer.snapshot(
        level=params["level"],
        service=params["service"],
        contains=params["q"],
        limit=limit,
    )
    return JSONResponse({
        "records": records,
        "count": len(records),
        "query": params["q"] or "",
        "logtail_configured": is_logtail_configured(),
    })
