"""Time-series snapshot store for the World State dashboard.

Every SNAPSHOT_INTERVAL_SEC (default 15 min) the background loop calls the
registered collector function, which returns a flat dict {metric_key: value}.
Each (key, ts, value) triple is stored in a single SQLite table.

The DB lives at ``snapshots.db`` next to this file. To persist across docker
rebuilds, mount that path as a volume (the file pattern is already in
``.dockerignore`` so it won't be baked into the image).

Storage cost is small: ~25 metrics × 96 snapshots/day × 365 = ~875k rows/year,
~50 MB on disk worst case.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Awaitable, Callable

DB_PATH = Path(__file__).parent / "snapshots.db"
SNAPSHOT_INTERVAL_SEC = 15 * 60  # 15 minutes

# Caller registers a collector that returns dict[str, float|int] of *current*
# metric values. Sync or async — both are accepted. See server.py for the
# concrete implementation.
CollectorFn = Callable[[], "dict | Awaitable[dict]"]
_collector: CollectorFn | None = None
_bg_task: asyncio.Task | None = None
_log = logging.getLogger("history")


# ── Schema ───────────────────────────────────────────────────────────────────
def init_db() -> None:
    """Create schema if not present. Idempotent — safe to call on every boot."""
    with sqlite3.connect(DB_PATH) as c:
        # WAL gives concurrent readers + a single writer without blocking.
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")  # WAL-safe and ~3× faster than FULL
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS metrics (
                ts    INTEGER NOT NULL,
                key   TEXT    NOT NULL,
                value REAL    NOT NULL,
                meta  TEXT,
                PRIMARY KEY (key, ts)
            ) WITHOUT ROWID
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts)")


# ── Write path ───────────────────────────────────────────────────────────────
def _insert_batch_blocking(rows: list[tuple]) -> int:
    if not rows:
        return 0
    with sqlite3.connect(DB_PATH) as c:
        c.executemany(
            "INSERT OR REPLACE INTO metrics(ts, key, value, meta) VALUES(?, ?, ?, ?)",
            rows,
        )
        return c.total_changes


async def record(metrics: dict, ts: int | None = None, meta: dict | None = None) -> int:
    """Persist one batch of metrics at a single timestamp.

    Non-numeric or None values are silently dropped. Returns rows written.
    """
    ts_int = int(ts) if ts is not None else int(time.time())
    meta_json = json.dumps(meta) if meta else None
    rows: list[tuple] = []
    for k, v in metrics.items():
        if v is None:
            continue
        try:
            rows.append((ts_int, str(k), float(v), meta_json))
        except (TypeError, ValueError):
            continue  # not numeric, skip
    return await asyncio.to_thread(_insert_batch_blocking, rows)


async def record_series(points: list[tuple]) -> int:
    """Persist a backfilled time series. ``points`` is a list of (ts, key, value)
    or (ts, key, value, meta_dict). Used by data_sources.py for historical imports.
    """
    rows: list[tuple] = []
    for p in points:
        if len(p) == 3:
            ts, key, value = p
            meta_json = None
        elif len(p) == 4:
            ts, key, value, meta = p
            meta_json = json.dumps(meta) if meta else None
        else:
            continue
        if value is None:
            continue
        try:
            rows.append((int(ts), str(key), float(value), meta_json))
        except (TypeError, ValueError):
            continue
    return await asyncio.to_thread(_insert_batch_blocking, rows)


# ── Read path ────────────────────────────────────────────────────────────────
def query(
    key: str,
    t_from: int,
    t_to: int,
    bucket_sec: int = 0,
) -> list[dict]:
    """Fetch a time-series for ``key`` between ``t_from`` and ``t_to``.

    If ``bucket_sec`` > 0, downsample with AVG over fixed-width buckets — useful
    for long ranges so charts don't pull tens of thousands of points.
    """
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        if bucket_sec and bucket_sec > 0:
            cur = c.execute(
                """
                SELECT (ts/?)*? AS ts, AVG(value) AS value, COUNT(*) AS n
                FROM metrics
                WHERE key = ? AND ts >= ? AND ts <= ?
                GROUP BY ts/?
                ORDER BY ts
                """,
                (bucket_sec, bucket_sec, key, t_from, t_to, bucket_sec),
            )
        else:
            cur = c.execute(
                """
                SELECT ts, value, 1 AS n FROM metrics
                WHERE key = ? AND ts >= ? AND ts <= ?
                ORDER BY ts
                """,
                (key, t_from, t_to),
            )
        return [dict(r) for r in cur.fetchall()]


def list_keys() -> list[dict]:
    """Return [{key, count, first_ts, last_ts}] for every metric in the DB."""
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(
            """
            SELECT key, COUNT(*) AS count, MIN(ts) AS first_ts, MAX(ts) AS last_ts
            FROM metrics
            GROUP BY key
            ORDER BY key
            """
        )
        return [dict(r) for r in cur.fetchall()]


def latest_snapshot() -> dict:
    """Return {key: {ts, value}} for the most recent value of every key."""
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(
            """
            SELECT m.key, m.ts, m.value
            FROM metrics m
            JOIN (SELECT key, MAX(ts) AS max_ts FROM metrics GROUP BY key) latest
              ON m.key = latest.key AND m.ts = latest.max_ts
            """
        )
        return {r["key"]: {"ts": r["ts"], "value": r["value"]} for r in cur.fetchall()}


# ── Background loop ──────────────────────────────────────────────────────────
def register_collector(fn: CollectorFn) -> None:
    """Register the function the loop will call to get current metric values."""
    global _collector
    _collector = fn


async def snapshot_now() -> int:
    """Run the collector once and persist the result. Returns rows written."""
    if _collector is None:
        _log.warning("no collector registered, skipping snapshot")
        return 0
    try:
        result = _collector()
        metrics = await result if asyncio.iscoroutine(result) else result
    except Exception as e:
        _log.warning("collector raised: %s", e)
        return 0
    if not isinstance(metrics, dict):
        _log.warning("collector returned %s, expected dict", type(metrics).__name__)
        return 0
    return await record(metrics)


async def _loop() -> None:
    # Initial snapshot so the DB has data immediately rather than after 15 min.
    try:
        n = await snapshot_now()
        _log.info("initial snapshot: %d metrics recorded", n)
    except Exception as e:
        _log.warning("initial snapshot failed: %s", e)

    while True:
        try:
            await asyncio.sleep(SNAPSHOT_INTERVAL_SEC)
            n = await snapshot_now()
            _log.debug("snapshot: %d metrics", n)
        except asyncio.CancelledError:
            break
        except Exception as e:
            _log.warning("loop iteration error: %s", e)


def start() -> None:
    """Start the background snapshot loop. Idempotent."""
    global _bg_task
    init_db()
    if _bg_task is None or _bg_task.done():
        _bg_task = asyncio.create_task(_loop())
        _log.info("snapshot loop started (interval=%ds, db=%s)", SNAPSHOT_INTERVAL_SEC, DB_PATH)


async def stop() -> None:
    """Cancel the loop and wait for it to finish."""
    global _bg_task
    if _bg_task and not _bg_task.done():
        _bg_task.cancel()
        try:
            await _bg_task
        except asyncio.CancelledError:
            pass
        _bg_task = None
