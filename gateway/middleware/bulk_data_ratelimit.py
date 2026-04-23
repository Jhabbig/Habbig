"""Bulk-data rate limit — the exfiltration backstop.

Sits in front of every list-returning JSON endpoint. Counts rows per user
per hour. Over budget → 429 + audit flag.

Budget (hard-coded; tune in code when real traffic dictates):

    ROW_BUDGET_HOUR    = 5000   rows / hour  → 429
    ROW_BUDGET_DAY     = 20000  rows / 24h   → flag for review (but allow
                                                the current request to pass
                                                so users see the warning)

Only JSON array responses (or JSON objects whose top-level ``items`` /
``results`` / ``predictions`` / ``markets`` / ``sources`` / ``rows`` field
is a list) count toward the budget. Everything else is passthrough.

Authenticated users only. Anonymous or gate-protected requests skip the
counter — they're already bounded by the global rate limiter.

Implementation note — counting rows means inspecting the response body.
Rather than stream-parsing JSON, we rely on ``starlette.responses.Response``
carrying its ``body`` bytes by the time middleware sees it. That's true
for JSONResponse (the only path that serves large list responses) but not
for StreamingResponse, which is skipped.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


log = logging.getLogger(__name__)


ROW_BUDGET_HOUR = 5000
ROW_BUDGET_DAY = 20000
SAMPLE_KEYS = ("items", "results", "predictions", "markets", "sources", "rows", "data")

# Paths that are list-heavy but user-scoped noise rather than sensitive data —
# excluded so we don't burn budget on e.g. notification polling.
_SKIP_PREFIXES = (
    "/_gateway_static",
    "/static",
    "/healthz",
    "/api/notifications/poll",
    "/api/security/capture-attempt",
)


def _count_rows(body: bytes) -> int:
    """Return the number of rows in a JSON body — 0 if not countable."""
    if not body:
        return 0
    try:
        data: Any = json.loads(body)
    except Exception:
        return 0
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in SAMPLE_KEYS:
            val = data.get(key)
            if isinstance(val, list):
                return len(val)
    return 0


def _hour_bucket(now: int) -> int:
    return (now // 3600) * 3600


def _resolve_user_id(request: Request) -> int | None:
    """Pull the authenticated user id off the request state."""
    state = getattr(request, "state", None)
    if not state:
        return None
    # Hardened-session middleware writes to state.user.
    user = getattr(state, "user", None)
    if isinstance(user, dict):
        uid = user.get("user_id") or user.get("id")
        if uid:
            return int(uid)
    # Impersonation: charge the admin for bulk fetches, not the target.
    imp = getattr(state, "impersonation", None)
    if imp:
        try:
            return int(imp.get("admin_user_id"))
        except Exception:
            return None
    return None


def _record_and_check(user_id: int, rows: int) -> tuple[bool, int, int]:
    """Increment counters; return (over_budget, hour_total, day_total).

    Over-budget is True if the just-added rows pushed us past
    ROW_BUDGET_HOUR this hour. Day total is tracked for flagging purposes.
    """
    import db as _db  # local so imports stay cheap for non-list endpoints

    now = int(time.time())
    hour_bucket = _hour_bucket(now)
    day_cutoff = now - 86400
    with _db.conn() as c:
        # Upsert current-hour counter.
        c.execute(
            "INSERT INTO bulk_fetch_counters "
            "(user_id, window_start, rows_fetched, endpoint_hits, flagged, last_updated) "
            "VALUES (?, ?, ?, 1, 0, ?) "
            "ON CONFLICT(user_id, window_start) DO UPDATE SET "
            "rows_fetched = rows_fetched + excluded.rows_fetched, "
            "endpoint_hits = endpoint_hits + 1, "
            "last_updated = excluded.last_updated",
            (user_id, hour_bucket, rows, now),
        )
        hour_row = c.execute(
            "SELECT rows_fetched FROM bulk_fetch_counters "
            "WHERE user_id = ? AND window_start = ?",
            (user_id, hour_bucket),
        ).fetchone()
        hour_total = int(hour_row["rows_fetched"]) if hour_row else rows

        day_row = c.execute(
            "SELECT COALESCE(SUM(rows_fetched), 0) AS total "
            "FROM bulk_fetch_counters "
            "WHERE user_id = ? AND window_start >= ?",
            (user_id, day_cutoff),
        ).fetchone()
        day_total = int(day_row["total"]) if day_row else hour_total

        if day_total > ROW_BUDGET_DAY:
            c.execute(
                "UPDATE bulk_fetch_counters SET flagged = 1 "
                "WHERE user_id = ? AND window_start = ?",
                (user_id, hour_bucket),
            )
    return hour_total > ROW_BUDGET_HOUR, hour_total, day_total


class BulkDataRateLimitMiddleware(BaseHTTPMiddleware):
    """Counts rows in JSON responses and enforces per-user hourly budget."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Cheap skip before any DB work.
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        response = await call_next(request)

        # Only JSON list responses participate.
        ct = response.headers.get("content-type", "")
        if "application/json" not in ct.lower():
            return response
        # Streaming responses don't expose .body synchronously.
        body = getattr(response, "body", None)
        if not body:
            return response
        rows = _count_rows(body)
        if rows < 20:
            return response

        user_id = _resolve_user_id(request)
        if not user_id:
            return response

        try:
            over, hour_total, day_total = _record_and_check(user_id, rows)
        except Exception as exc:
            log.warning("bulk_data counter failed uid=%s: %s", user_id, exc)
            return response

        # Attach observability headers so the client sees the remaining
        # budget — useful for honest UIs that want to tell the user.
        remaining = max(0, ROW_BUDGET_HOUR - hour_total)
        response.headers["X-Bulk-Rows-Remaining"] = str(remaining)
        response.headers["X-Bulk-Rows-Day"] = str(day_total)

        if over:
            reset_at = _hour_bucket(int(time.time())) + 3600
            log.warning(
                "bulk-fetch 429 user_id=%s rows_in_hour=%s day=%s",
                user_id, hour_total, day_total,
            )
            return JSONResponse(
                {"error": "Hourly data budget exceeded.",
                 "rows_in_hour": hour_total,
                 "budget": ROW_BUDGET_HOUR,
                 "reset_at": reset_at},
                status_code=429,
                headers={
                    "Retry-After": str(max(1, reset_at - int(time.time()))),
                    "X-Bulk-Rows-Remaining": "0",
                },
            )
        return response
