"""Historical correlation between prediction markets.

Loads the last 90 days of ``market_snapshots`` rows per market and
computes a Pearson correlation coefficient over price-change deltas
(not raw prices — raw-price correlation is dominated by the base level
and over-weights every pair that happens to sit around 50%).

Public surface:

  pearson(xs, ys)                  pure function
  align_snapshot_series(a, b)      resample both series to a shared grid
  compute_market_correlations(
    anchor_slug, *, min_abs=0.25,
    days=90, limit=50,
  )                                anchor → [{market, correlation, category, ...}]

Everything is async so the caller can await alongside the existing
cache layer. Cache key: ``scenario:corr:<anchor_slug>:<days>:<min_abs>``
with a 24h TTL. The cache is best-effort — a miss just runs the query
again.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Iterable, Optional


log = logging.getLogger("scenarios.correlation")


DEFAULT_DAYS = 90
DEFAULT_MIN_ABS = 0.25
DEFAULT_LIMIT = 50
DEFAULT_GRID_INTERVAL = 3600  # 1 hour


# ── DB path ─────────────────────────────────────────────────────────────────


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


# ── Pure maths ──────────────────────────────────────────────────────────────


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation for two equal-length series.

    Returns None when inputs are too short (<3 points), unequal lengths,
    or either series has zero variance. No numpy dependency — keeps the
    module usable from contexts where numpy isn't installed (CI, the
    scraper, etc.).
    """
    if not xs or not ys or len(xs) != len(ys):
        return None
    n = len(xs)
    if n < 3:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sum_sq_x = 0.0
    sum_sq_y = 0.0
    sum_xy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mean_x
        dy = y - mean_y
        sum_sq_x += dx * dx
        sum_sq_y += dy * dy
        sum_xy += dx * dy
    denom = math.sqrt(sum_sq_x * sum_sq_y)
    if denom <= 0:
        return None
    r = sum_xy / denom
    # Clamp — rounding can push it to 1.0000000002 on identical series.
    return max(-1.0, min(1.0, r))


def align_snapshot_series(
    a: list[tuple[int, float]],
    b: list[tuple[int, float]],
    *,
    interval_seconds: int = DEFAULT_GRID_INTERVAL,
) -> tuple[list[float], list[float]]:
    """Resample two (ts, price) series to a shared hourly grid.

    Uses last-observation-carried-forward: for each grid timestamp,
    picks the most recent observation ≤ that timestamp from each side.
    Grid starts at max(first_ts_a, first_ts_b) and ends at
    min(last_ts_a, last_ts_b) — so both series are defined everywhere.

    Returns ``([], [])`` if there's no overlap. The aligned arrays are
    suitable for ``pearson``.
    """
    if not a or not b or interval_seconds <= 0:
        return [], []
    a_sorted = sorted(a, key=lambda t: t[0])
    b_sorted = sorted(b, key=lambda t: t[0])
    start = max(a_sorted[0][0], b_sorted[0][0])
    end = min(a_sorted[-1][0], b_sorted[-1][0])
    if end <= start:
        return [], []

    def _locf(series: list[tuple[int, float]], t: int) -> Optional[float]:
        # Binary search for largest ts ≤ t.
        lo, hi = 0, len(series) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if series[mid][0] <= t:
                best = series[mid][1]
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    xs: list[float] = []
    ys: list[float] = []
    t = start
    while t <= end:
        va = _locf(a_sorted, t)
        vb = _locf(b_sorted, t)
        if va is not None and vb is not None:
            xs.append(float(va))
            ys.append(float(vb))
        t += interval_seconds
    return xs, ys


def deltas(series: list[float]) -> list[float]:
    """Successive differences. Correlating price changes > raw prices
    because it decouples the result from the absolute level of each
    market (two markets can both hover near 50% without being correlated).
    """
    if len(series) < 2:
        return []
    return [series[i] - series[i - 1] for i in range(1, len(series))]


def _volatility(series: list[float]) -> float:
    """stdev of ``series`` — used to scale the scenario's expected shift."""
    if len(series) < 2:
        return 0.0
    try:
        return statistics.pstdev(series)
    except statistics.StatisticsError:
        return 0.0


# ── DB readers ──────────────────────────────────────────────────────────────


def _load_snapshots(conn: sqlite3.Connection, slug: str, since_ts: int) -> list[tuple[int, float]]:
    """Return (ts, yes_price) for a market since ``since_ts``, ordered."""
    if not slug:
        return []
    rows = conn.execute(
        "SELECT snapshotted_at AS ts, yes_price AS y FROM market_snapshots "
        "WHERE market_slug = ? AND snapshotted_at >= ? "
        "ORDER BY snapshotted_at ASC",
        (slug, int(since_ts)),
    ).fetchall()
    return [(int(r["ts"]), float(r["y"])) for r in rows if r["y"] is not None]


def _load_active_markets(conn: sqlite3.Connection, since_ts: int) -> list[dict]:
    """Return one row per market with at least one snapshot in the window.

    Picks the newest row for the metadata (market_question, category,
    yes_price). Uses window-function-free SQL so this works on sqlite
    versions as old as 3.24.
    """
    rows = conn.execute(
        """
        SELECT s1.market_slug,
               s1.market_question,
               s1.category,
               s1.yes_price,
               s1.snapshotted_at,
               COUNT(s2.id) AS sample_count
        FROM market_snapshots s1
        LEFT JOIN market_snapshots s2
          ON s2.market_slug = s1.market_slug
          AND s2.snapshotted_at >= ?
        WHERE s1.market_slug IN (
            SELECT market_slug FROM market_snapshots
            WHERE snapshotted_at >= ?
            GROUP BY market_slug
        )
        GROUP BY s1.market_slug
        HAVING s1.snapshotted_at = MAX(s1.snapshotted_at)
        """,
        (int(since_ts), int(since_ts)),
    ).fetchall()
    return [
        {
            "slug": r["market_slug"],
            "question": r["market_question"] or r["market_slug"],
            "category": r["category"],
            "current_price": float(r["yes_price"]) if r["yes_price"] is not None else None,
            "sample_count": int(r["sample_count"] or 0),
        }
        for r in rows
    ]


# ── Public entrypoint ──────────────────────────────────────────────────────


async def compute_market_correlations(
    anchor_slug: str,
    *,
    min_abs: float = DEFAULT_MIN_ABS,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    use_cache: bool = True,
) -> list[dict]:
    """Correlation of *anchor_slug* against every other active market.

    Filters to |r| ≥ ``min_abs`` (default 0.25) so the UI doesn't drown
    in noise. Sorted by |r| desc, capped at ``limit``.

    Each dict:
      {
        slug, question, category,
        current_price,
        correlation,       # Pearson r on hourly deltas
        sample_size,       # aligned points used for r
        volatility,        # stdev of the other market's deltas
      }
    """
    if not anchor_slug:
        return []

    cache_key = f"scenario:corr:{anchor_slug}:{days}:{min_abs}:{limit}"
    if use_cache:
        try:
            from cache.ttl import ttl_cache  # hot-path, sync
            cached = ttl_cache.get(cache_key)
            if cached is not None:
                return cached
        except Exception:
            pass

    since_ts = int(time.time()) - days * 86400
    conn = _connect()
    try:
        anchor_hist = _load_snapshots(conn, anchor_slug, since_ts)
        if len(anchor_hist) < 3:
            return []
        active = _load_active_markets(conn, since_ts)
        results: list[dict] = []
        for m in active:
            if m["slug"] == anchor_slug:
                continue
            other_hist = _load_snapshots(conn, m["slug"], since_ts)
            if len(other_hist) < 3:
                continue
            a_prices, b_prices = align_snapshot_series(anchor_hist, other_hist)
            if len(a_prices) < 4:  # need at least 3 deltas
                continue
            a_d = deltas(a_prices)
            b_d = deltas(b_prices)
            r = pearson(a_d, b_d)
            if r is None or abs(r) < min_abs:
                continue
            results.append({
                "slug": m["slug"],
                "question": m["question"],
                "category": m["category"],
                "current_price": m["current_price"],
                "correlation": round(r, 4),
                "sample_size": len(a_prices),
                "volatility": round(_volatility(b_d), 6),
            })
    finally:
        conn.close()

    results.sort(key=lambda e: abs(e["correlation"] or 0), reverse=True)
    results = results[: int(limit)]

    if use_cache:
        try:
            from cache.ttl import ttl_cache
            ttl_cache.set(cache_key, results, ttl_seconds=86400)
        except Exception:
            pass

    return results
