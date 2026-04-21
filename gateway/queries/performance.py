"""Admin-dashboard data accessors for the slow-query log.

Consumed by the /admin/performance route (wired up separately — that
diff lives in ``admin_routes.py``, outside the scope of this session).
Every function returns plain dicts / lists so the route handler can
serialize them directly.

The admin page layout these feed:

  * ``top_slow_shapes(hours=24, limit=20)``
      Top N slowest *query shapes* (grouped by ``query_signature``)
      with count + avg_ms + p95_ms + max_ms. Ordered by avg desc so
      the worst recurring offender floats to the top.

  * ``slow_query_histogram(hours=24)``
      Count of traces per 100 ms bucket. Admin renders as a bar chart
      so a single bad release shows up as a spike.

  * ``endpoint_percentiles(hours=24)``
      P50/P95/P99 duration per ``endpoint``. Uses a two-pass approach
      (fetch all rows for the endpoint, sort in-memory) rather than a
      SQL percentile window because sqlite3 has no PERCENTILE_CONT. The
      table is bounded by retention so pulling everything is cheap.

  * ``trim_slow_query_log(keep_days=30)``
      Retention: delete rows older than *keep_days*. Called by a daily
      cron (also outside this module's scope).
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from typing import Optional

import db


def _window_cutoff(hours: int) -> int:
    return int(time.time()) - hours * 3600


def top_slow_shapes(hours: int = 24, limit: int = 20) -> list[dict]:
    """Return the worst recurring query shapes in the given window.

    Returned rows:
        query_signature, example_query, count, avg_ms, p95_ms, max_ms.

    Ordered by avg_ms DESC. ``example_query`` is the most recent full
    query text matching the signature so a reviewer can eyeball the
    actual SQL without an extra lookup."""
    cutoff = _window_cutoff(hours)
    with db.conn() as c:
        # Bucket aggregates + a pulled sample text per signature. We
        # pull the most-recent full query as the representative example
        # (MAX(timestamp) is indexed so this is cheap).
        rows = c.execute(
            """
            SELECT query_signature,
                   COUNT(*)             AS count,
                   AVG(duration_ms)     AS avg_ms,
                   MAX(duration_ms)     AS max_ms,
                   MAX(timestamp)       AS last_seen_ts
              FROM slow_query_log
             WHERE timestamp >= ?
             GROUP BY query_signature
             ORDER BY avg_ms DESC
             LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()

        out: list[dict] = []
        for r in rows:
            sig = r["query_signature"]
            # P95 = second pass, in-memory sort (count is small per sig).
            durations = [
                int(d[0]) for d in c.execute(
                    "SELECT duration_ms FROM slow_query_log "
                    "WHERE query_signature = ? AND timestamp >= ? "
                    "ORDER BY duration_ms",
                    (sig, cutoff),
                ).fetchall()
            ]
            p95 = _percentile(durations, 0.95)
            # Example text: most recent hit.
            example = c.execute(
                "SELECT query FROM slow_query_log "
                "WHERE query_signature = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (sig,),
            ).fetchone()
            out.append({
                "query_signature": sig,
                "example_query": example["query"] if example else "",
                "count": int(r["count"]),
                "avg_ms": round(float(r["avg_ms"]), 1),
                "p95_ms": int(p95),
                "max_ms": int(r["max_ms"]),
                "last_seen_ts": int(r["last_seen_ts"] or 0),
            })
    return out


def slow_query_histogram(hours: int = 24, bucket_ms: int = 100) -> list[dict]:
    """Histogram of trace counts per ``bucket_ms`` duration bucket.

    Returns a dense list [{bucket_ms, count}, …] covering every bucket
    from 0 up to the largest observed. Missing buckets are returned as
    zeros so the admin chart doesn't have gaps."""
    cutoff = _window_cutoff(hours)
    with db.conn() as c:
        rows = c.execute(
            f"""
            SELECT (duration_ms / {int(bucket_ms)}) * {int(bucket_ms)} AS bucket,
                   COUNT(*) AS count
              FROM slow_query_log
             WHERE timestamp >= ?
             GROUP BY bucket
             ORDER BY bucket
            """,
            (cutoff,),
        ).fetchall()
    if not rows:
        return []
    counts = {int(r["bucket"]): int(r["count"]) for r in rows}
    top = max(counts.keys())
    return [
        {"bucket_ms": b, "count": counts.get(b, 0)}
        for b in range(0, top + bucket_ms, bucket_ms)
    ]


def endpoint_percentiles(hours: int = 24) -> list[dict]:
    """P50/P95/P99 per endpoint over the window.

    NULL endpoints (background jobs, cron tasks, anything that didn't
    set request context) are grouped under the literal key "(job)" so
    they stay visible instead of collapsing silently."""
    cutoff = _window_cutoff(hours)
    with db.conn() as c:
        rows = c.execute(
            "SELECT endpoint, duration_ms FROM slow_query_log "
            "WHERE timestamp >= ?",
            (cutoff,),
        ).fetchall()
    by_endpoint: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        key = r["endpoint"] or "(job)"
        by_endpoint[key].append(int(r["duration_ms"]))

    out: list[dict] = []
    for endpoint, durations in by_endpoint.items():
        durations.sort()
        out.append({
            "endpoint": endpoint,
            "count": len(durations),
            "p50_ms": int(_percentile(durations, 0.50)),
            "p95_ms": int(_percentile(durations, 0.95)),
            "p99_ms": int(_percentile(durations, 0.99)),
            "max_ms": durations[-1],
        })
    # Most painful endpoints first, by P95.
    out.sort(key=lambda r: r["p95_ms"], reverse=True)
    return out


def trim_slow_query_log(keep_days: int = 30) -> int:
    """Delete rows older than *keep_days*. Returns rowcount deleted."""
    cutoff = int(time.time()) - keep_days * 86400
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM slow_query_log WHERE timestamp < ?",
            (cutoff,),
        )
        return cur.rowcount


def overall_stats(hours: int = 24) -> dict:
    """Single-card summary for the top of the admin page."""
    cutoff = _window_cutoff(hours)
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n, "
            "       COALESCE(AVG(duration_ms), 0) AS avg_ms, "
            "       COALESCE(MAX(duration_ms), 0) AS max_ms, "
            "       MIN(timestamp) AS first_ts, "
            "       MAX(timestamp) AS last_ts "
            "FROM slow_query_log WHERE timestamp >= ?",
            (cutoff,),
        ).fetchone()
    return {
        "window_hours": hours,
        "total_slow_queries": int(row["n"] or 0),
        "avg_ms": round(float(row["avg_ms"] or 0), 1),
        "max_ms": int(row["max_ms"] or 0),
        "first_ts": int(row["first_ts"] or 0),
        "last_ts": int(row["last_ts"] or 0),
    }


# ── Percentile helper ────────────────────────────────────────────────

def _percentile(sorted_values: list[int], q: float) -> float:
    """Linear-interpolation percentile matching numpy's default.

    Returns 0 on empty input — the caller contextually knows "no data"
    so we don't want to introduce a None into the dict payload."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    # Input may already be sorted (top_slow_shapes passes a sorted
    # array) but sorting again is O(n log n) on a tiny list and keeps
    # the call-sites simple.
    values = sorted(sorted_values)
    k = (len(values) - 1) * q
    f = int(k)
    c_idx = min(f + 1, len(values) - 1)
    return values[f] + (values[c_idx] - values[f]) * (k - f)
