"""Lightweight, in-process performance counters.

Two distinct stores, both thread-safe and bounded in size so a long-running
process never balloons:

* **query_stats** — per-statement timings from the instrumented sqlite3
  connection. Buckets by a normalised "shape" (first ~80 chars of the SQL
  with literals stripped) and keeps the N slowest individual executions.
* **endpoint_stats** — per-HTTP-route timings recorded by the perf
  middleware. Buckets by "METHOD path" with path params collapsed (e.g.
  `GET /api/credibility/{h}`).

Both stores expose a `snapshot()` used by `/admin/performance`. Stats are
per-worker — multi-worker deployments see one slice per uvicorn worker, same
as the rate limiter's in-memory fallback. Cross-process aggregation would
require exporting counters to Redis; not worth it until we actually run
multi-worker.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Any, Optional

log = logging.getLogger("perf")


# Queries slower than this emit a structured warning log. Tuned to catch
# real pathologies without spamming on legitimate analytics scans.
SLOW_QUERY_THRESHOLD_SEC = 1.0

# Requests slower than this emit a warning. 2s is the threshold at which a
# user perceives "this site is broken" — worth alerting on.
SLOW_REQUEST_THRESHOLD_SEC = 2.0

# Ring buffer of individual slow executions. Bounded so an outage that
# causes every query to be slow can't OOM the process.
_MAX_SLOW_SAMPLES = 200

# Normalisation: collapse any string/number literal so `WHERE id = 42` and
# `WHERE id = 7` bucket together.
_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\b\d+\b|\?")


def _normalise_sql(sql: str) -> str:
    sql = sql.strip()
    sql = _LITERAL_RE.sub("?", sql)
    sql = " ".join(sql.split())
    return sql[:200]


# Path normalisation: `/api/credibility/sho` and `/api/credibility/julian`
# both bucket under `/api/credibility/{h}`. The catch-all prefix takes care
# of weird paths we haven't mapped yet.
_PATH_PARAM_RE = re.compile(r"/(\d+|[0-9a-f]{8,}|[A-Za-z0-9_.-]{24,})(?=/|$)")


def _normalise_path(path: str) -> str:
    # Drop query string if any slipped through
    path = path.split("?", 1)[0]
    path = _PATH_PARAM_RE.sub("/{p}", path)
    # Known templated segments that don't look like IDs but should still
    # collapse — source handles, market slugs — roll under the last segment.
    if path.startswith("/api/credibility/"):
        path = "/api/credibility/{h}" + path[len("/api/credibility/x"):]
        path = path.rstrip("/")
        # calibration sub-route preserved
        if path.endswith("/calibration"):
            return "/api/credibility/{h}/calibration"
        return "/api/credibility/{h}"
    if path.startswith("/sources/") and path.count("/") == 2:
        return "/sources/{h}"
    return path


class _QueryStats:
    def __init__(self) -> None:
        self._lock = Lock()
        self._totals: dict[str, dict[str, float]] = defaultdict(
            lambda: {"count": 0, "total_seconds": 0.0, "max_seconds": 0.0}
        )
        self._slow: deque = deque(maxlen=_MAX_SLOW_SAMPLES)

    def record(self, sql: str, duration_seconds: float) -> None:
        shape = _normalise_sql(sql)
        with self._lock:
            bucket = self._totals[shape]
            bucket["count"] += 1
            bucket["total_seconds"] += duration_seconds
            if duration_seconds > bucket["max_seconds"]:
                bucket["max_seconds"] = duration_seconds

            if duration_seconds >= SLOW_QUERY_THRESHOLD_SEC:
                self._slow.append(
                    {
                        "sql": sql[:500],
                        "duration_seconds": round(duration_seconds, 4),
                        "ts": int(time.time()),
                    }
                )
        if duration_seconds >= SLOW_QUERY_THRESHOLD_SEC:
            log.warning(
                "slow_query",
                extra={
                    "duration_seconds": round(duration_seconds, 4),
                    "statement": sql[:500],
                },
            )

    def snapshot(self, limit: int = 20) -> dict[str, Any]:
        with self._lock:
            rows = [
                {
                    "shape": shape,
                    "count": data["count"],
                    "total_seconds": round(data["total_seconds"], 4),
                    "avg_seconds": round(
                        data["total_seconds"] / data["count"], 4
                    ) if data["count"] else 0.0,
                    "max_seconds": round(data["max_seconds"], 4),
                }
                for shape, data in self._totals.items()
            ]
            slow = list(self._slow)
        rows.sort(key=lambda r: r["total_seconds"], reverse=True)
        return {
            "top_by_total_time": rows[:limit],
            "recent_slow_queries": slow[-limit:][::-1],
            "slow_query_threshold_seconds": SLOW_QUERY_THRESHOLD_SEC,
        }

    def reset(self) -> None:
        with self._lock:
            self._totals.clear()
            self._slow.clear()


class _EndpointStats:
    def __init__(self) -> None:
        self._lock = Lock()
        self._totals: dict[str, dict[str, float]] = defaultdict(
            lambda: {
                "count": 0,
                "total_seconds": 0.0,
                "max_seconds": 0.0,
                "status_2xx": 0,
                "status_4xx": 0,
                "status_5xx": 0,
            }
        )
        self._slow: deque = deque(maxlen=_MAX_SLOW_SAMPLES)

    def record(
        self,
        method: str,
        path: str,
        duration_seconds: float,
        status_code: int,
    ) -> None:
        key = f"{method} {_normalise_path(path)}"
        with self._lock:
            bucket = self._totals[key]
            bucket["count"] += 1
            bucket["total_seconds"] += duration_seconds
            if duration_seconds > bucket["max_seconds"]:
                bucket["max_seconds"] = duration_seconds
            if 200 <= status_code < 300:
                bucket["status_2xx"] += 1
            elif 400 <= status_code < 500:
                bucket["status_4xx"] += 1
            elif status_code >= 500:
                bucket["status_5xx"] += 1

            if duration_seconds >= SLOW_REQUEST_THRESHOLD_SEC:
                self._slow.append(
                    {
                        "endpoint": key,
                        "duration_seconds": round(duration_seconds, 4),
                        "status": status_code,
                        "ts": int(time.time()),
                    }
                )
        if duration_seconds >= SLOW_REQUEST_THRESHOLD_SEC:
            log.warning(
                "slow_request",
                extra={
                    "endpoint": key,
                    "duration_seconds": round(duration_seconds, 4),
                    "status_code": status_code,
                },
            )

    def snapshot(self, limit: int = 20) -> dict[str, Any]:
        with self._lock:
            rows = [
                {
                    "endpoint": key,
                    "count": data["count"],
                    "total_seconds": round(data["total_seconds"], 4),
                    "avg_seconds": round(
                        data["total_seconds"] / data["count"], 4
                    ) if data["count"] else 0.0,
                    "max_seconds": round(data["max_seconds"], 4),
                    "status_2xx": data["status_2xx"],
                    "status_4xx": data["status_4xx"],
                    "status_5xx": data["status_5xx"],
                }
                for key, data in self._totals.items()
            ]
            slow = list(self._slow)
        rows.sort(key=lambda r: r["avg_seconds"], reverse=True)
        total_req = sum(r["count"] for r in rows)
        total_time = sum(r["total_seconds"] for r in rows)
        avg = round(total_time / total_req, 4) if total_req else 0.0
        return {
            "top_by_avg_latency": rows[:limit],
            "recent_slow_requests": slow[-limit:][::-1],
            "total_requests": total_req,
            "avg_seconds": avg,
            "slow_request_threshold_seconds": SLOW_REQUEST_THRESHOLD_SEC,
        }

    def reset(self) -> None:
        with self._lock:
            self._totals.clear()
            self._slow.clear()


# Module-level singletons. db.py and the middleware both import these.
query_stats = _QueryStats()
endpoint_stats = _EndpointStats()


def reset_all() -> None:
    """Called by tests and the admin reset button."""
    query_stats.reset()
    endpoint_stats.reset()
