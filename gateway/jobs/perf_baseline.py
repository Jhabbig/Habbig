"""Nightly perf-baseline snapshot.

Runs a representative set of hot-read queries against the live DB and
stores the resulting (p50, p95, p99, max) per endpoint in
``perf_baseline_snapshots``. The admin dashboard reads the table to
render a 30-day sparkline per endpoint and alert when any 7-day p95
regresses > 20% from the prior week.

Design notes:

* **In-process, not over HTTP.** The prompt's `perf_baseline.py`
  reference runs an httpx client against localhost. That's fine for
  a one-shot baseline but wrong for a cron: it doubles measurement
  cost (HTTP framing + middleware chain) and requires the job to
  hold a live session cookie. We snapshot the DB query itself —
  that's what actually regresses under schema drift, index loss,
  or N+1 creep. Middleware-level latency is already captured by
  ``slow_request_log``.

* **Sampled, not profiled.** 30 samples per endpoint keeps each
  night's run under a few seconds and produces stable percentile
  estimates. Bumping to 100 gives marginal accuracy improvements
  and costs 3× as much.

* **Error-tolerant.** A single failing endpoint (e.g. a feature-
  flagged query that needs a user_id we don't have) logs + skips
  to the next. An empty table + non-zero error_count is visible
  on the admin page as "endpoint recently broken" — the more
  useful surface than silently omitting the row.

* **Regression alerting.** Written alongside the snapshot write:
  compare this run's p95 to the median-of-last-7 from the same
  endpoint. If it's > 1.2× and the prior median was > 50 ms (so
  small absolute jumps don't fire), log at ERROR so the on-call
  log tail catches it.
"""

from __future__ import annotations

import logging
import statistics
import time
from typing import Any, Callable

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.perf_baseline")


# Samples per endpoint. Tuned so the full cron run stays under 10s
# on a production box with a warm SQLite page cache.
_SAMPLE_COUNT = 30

# Minimum prior-median latency (ms) before the regression alert fires.
# Below this, absolute noise dominates multiplicative ratios — a jump
# from 2 ms → 8 ms is a 4× "regression" but not actionable.
_ALERT_MIN_PRIOR_P95_MS = 50.0

# Regression multiplier: today's p95 must exceed _ALERT_MULTIPLIER ×
# the 7-day median of prior p95s to fire.
_ALERT_MULTIPLIER = 1.20


# ── Endpoints we sample ───────────────────────────────────────────────────
#
# Each entry is a (name, callable) pair where the callable takes no
# arguments and returns the query's result (which we discard). Name is
# what gets persisted as ``endpoint`` — keep stable across releases
# because the admin page groups by it.
#
# We pull these from queries/ rather than from the HTTP surface so the
# baseline measures what changes when indexes land or queries grow
# (migration 080 etc.), not the middleware stack.

def _build_probes() -> list[tuple[str, Callable[[], Any]]]:
    """Return the canonical hot-read probes.

    Resolution rule: every probe MUST be a function that lives on ``db``
    (the canonical SQLite layer). The earlier ``queries.*`` shims were
    listed by name but several of them have been renamed away from
    list_recent_* during the queries/ decomposition, and probing a
    moving target burns 30 calls per probe per night logging
    AttributeError. Probes here track the set of read paths that route
    handlers actually call — see PERFORMANCE_BASELINE.md for which
    endpoints they back.
    """
    import db as _db

    probes: list[tuple[str, Callable[[], Any]]] = []

    # /api/feed + /dashboards landing — the headline list view.
    probes.append((
        "db.list_recent_predictions",
        lambda: _db.list_recent_predictions(limit=50),
    ))

    # Resolution job's primary scan — every cron tick walks this.
    probes.append((
        "db.get_unresolved_market_ids",
        lambda: _db.get_unresolved_market_ids(),
    ))

    # FTS5 endpoints — used by ⌘K and the global search box. A short
    # query so we don't accidentally measure FTS planning cost on a
    # 200-char input.
    probes.append((
        "db.search_predictions",
        lambda: _db.search_predictions("fed", limit=10),
    ))
    probes.append((
        "db.search_markets",
        lambda: _db.search_markets("fed", limit=10),
    ))
    probes.append((
        "db.search_sources",
        lambda: _db.search_sources("fed", limit=10),
    ))

    return probes


def _sample_one(fn: Callable[[], Any], count: int) -> tuple[list[float], int]:
    """Return (durations_ms, error_count) for `count` invocations."""
    durations: list[float] = []
    errors = 0
    for _ in range(count):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception:
            errors += 1
            continue
        durations.append((time.perf_counter() - t0) * 1000.0)
    return durations, errors


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if p <= 0:
        return sorted_values[0]
    if p >= 100:
        return sorted_values[-1]
    # Nearest-rank — cheap and deterministic; the 1-row wobble relative
    # to linear interpolation doesn't matter at n=30.
    idx = int(round((p / 100.0) * (len(sorted_values) - 1)))
    return sorted_values[idx]


def _prior_median_p95(endpoint: str, lookback_days: int = 7) -> float | None:
    """Median p95 of the last `lookback_days` snapshots for an endpoint.

    Returns None when we don't have enough history to alert on — first
    week of a new endpoint will pass through silently, which is what
    we want.
    """
    import db
    cutoff = int(time.time()) - lookback_days * 86400
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT p95_ms FROM perf_baseline_snapshots "
                "WHERE endpoint = ? AND timestamp >= ? AND p95_ms IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT ?",
                (endpoint, cutoff, lookback_days),
            ).fetchall()
    except Exception:
        return None
    vals = [float(r["p95_ms"]) for r in rows if r["p95_ms"] is not None]
    if len(vals) < 3:
        # Not enough data points for a stable median. Wait for a
        # backfill week before alerting.
        return None
    return statistics.median(vals)


def _persist(
    endpoint: str,
    durations: list[float],
    errors: int,
) -> None:
    import db
    sorted_ds = sorted(durations)
    p50 = _percentile(sorted_ds, 50) if sorted_ds else None
    p95 = _percentile(sorted_ds, 95) if sorted_ds else None
    p99 = _percentile(sorted_ds, 99) if sorted_ds else None
    mx = sorted_ds[-1] if sorted_ds else None
    try:
        with db.conn() as c:
            c.execute(
                "INSERT INTO perf_baseline_snapshots ("
                "  timestamp, endpoint, sample_count, "
                "  p50_ms, p95_ms, p99_ms, max_ms, error_count"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()), endpoint, len(durations),
                    p50, p95, p99, mx, errors,
                ),
            )
    except Exception as e:
        log.warning("perf_baseline persist failed for %s: %s", endpoint, e)

    # Regression alert: compare today's p95 to the 7-day median of
    # prior p95s. Only fire when prior is already "slow enough" that
    # a multiplicative jump is meaningful.
    if p95 is not None:
        prior = _prior_median_p95(endpoint, lookback_days=7)
        if (
            prior is not None
            and prior >= _ALERT_MIN_PRIOR_P95_MS
            and p95 >= _ALERT_MULTIPLIER * prior
        ):
            log.error(
                "perf_regression: endpoint=%s p95_today=%.1fms "
                "p95_prior7d_median=%.1fms ratio=%.2fx",
                endpoint, p95, prior, p95 / prior,
            )


@register_job("run_perf_baseline")
async def run_perf_baseline() -> dict[str, Any]:
    """Sample every probe N times, persist percentiles, alert on
    regression. Returns a summary dict the worker can log."""
    probes = _build_probes()
    if not probes:
        return {"ok": True, "probes": 0, "skipped": "no probes available"}

    total_errors = 0
    results: list[dict[str, Any]] = []
    for name, fn in probes:
        durations, errors = _sample_one(fn, _SAMPLE_COUNT)
        total_errors += errors
        _persist(name, durations, errors)
        if durations:
            sorted_ds = sorted(durations)
            results.append({
                "endpoint": name,
                "p50_ms": round(_percentile(sorted_ds, 50), 2),
                "p95_ms": round(_percentile(sorted_ds, 95), 2),
                "error_count": errors,
            })
        else:
            results.append({
                "endpoint": name,
                "p50_ms": None,
                "p95_ms": None,
                "error_count": errors,
            })
    log.info(
        "perf_baseline: %d probes, %d total errors, snapshots written",
        len(probes), total_errors,
    )
    return {
        "ok": True,
        "probes": len(probes),
        "total_errors": total_errors,
        "results": results,
    }


# 03:20 UTC daily. Sits before trim_perf_logs (03:40) so the nightly
# snapshot never gets trimmed by its own retention job.
register_cron("run_perf_baseline", hour=3, minute=20)
