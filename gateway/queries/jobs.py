"""Queries for the background-job admin dashboard at /admin/jobs.

Reads from ``job_runs`` (created by migration 105) and introspects the
APScheduler-backed singleton in ``scheduler.scheduler``. Pure read-only —
mutations (pause/resume/trigger) live on the scheduler object itself and
are invoked from the route layer.

Three public surfaces:

  * ``list_recent_job_runs(limit)`` — most recent runs across every job.
  * ``list_cron_schedule()``        — registered cron / interval jobs with
                                       next-run + last-run + success rate.
  * ``get_job_stats(window_hours)`` — aggregate counters for the stats bar.

Column shape note: the migration uses ``ok`` (0/1) + ``completed_at`` +
``error``. The admin UI talks about ``status`` (success/failed/retrying)
and ``finished_at`` and ``error_message`` — this module is the
translation layer so the template can stay decoupled from the storage
shape.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import db

log = logging.getLogger("queries.jobs")


def _status_from_row(row: Any) -> str:
    """Translate the ``ok`` column into the human-readable status string.

    ``ok IS NULL`` means the run is still in flight (started_at set,
    completed_at not set yet) — we surface that as ``running``.
    """
    if row is None:
        return "unknown"
    ok = row["ok"] if "ok" in row.keys() else None
    completed_at = row["completed_at"] if "completed_at" in row.keys() else None
    if ok is None and completed_at is None:
        return "running"
    if ok == 1:
        return "success"
    return "failed"


def list_recent_job_runs(
    limit: int = 100,
    *,
    job_name: Optional[str] = None,
) -> list[dict]:
    """Return up to *limit* most recent job runs, newest first.

    Each row is a dict with: ``id``, ``job_name``, ``status``,
    ``started_at``, ``finished_at``, ``duration_ms``, ``error_message``,
    ``triggered_by``, ``attempt`` (always 1 — the scheduler doesn't
    retry; the column exists for forward-compat with arq).
    """
    limit = max(1, min(int(limit), 500))
    try:
        with db.conn() as c:
            if job_name:
                rows = c.execute(
                    "SELECT id, job_name, started_at, completed_at, "
                    "duration_ms, ok, error, triggered_by "
                    "FROM job_runs WHERE job_name = ? "
                    "ORDER BY started_at DESC LIMIT ?",
                    (job_name, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, job_name, started_at, completed_at, "
                    "duration_ms, ok, error, triggered_by "
                    "FROM job_runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
    except Exception:
        log.exception("list_recent_job_runs failed")
        return []

    out: list[dict] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "job_name": r["job_name"],
            "status": _status_from_row(r),
            "started_at": r["started_at"],
            "finished_at": r["completed_at"],
            "duration_ms": r["duration_ms"],
            "error_message": r["error"] or None,
            "triggered_by": r["triggered_by"],
            "attempt": 1,
        })
    return out


def list_currently_running(limit: int = 20) -> list[dict]:
    """Return rows where ``ok IS NULL AND completed_at IS NULL`` — i.e.
    the scheduler has recorded a start but not yet an end.

    A run lingering here for more than ~1h is almost certainly a crashed
    worker; the page surfaces an "age" so an admin can spot it.
    """
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT id, job_name, started_at, triggered_by "
                "FROM job_runs "
                "WHERE ok IS NULL AND completed_at IS NULL "
                "ORDER BY started_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
    except Exception:
        log.exception("list_currently_running failed")
        return []
    return [dict(r) for r in rows]


def list_cron_schedule() -> list[dict]:
    """Return one row per registered scheduler job.

    Pulls metadata from the in-process ``Scheduler`` (trigger expression,
    next-run, paused flag) and joins it with per-job aggregates from
    ``job_runs`` (last_run, last_status, 24h success rate). The page
    sorts these by name so the order is deterministic across refreshes.
    """
    try:
        from scheduler import scheduler as sched
        metadata = sched.jobs_metadata()
    except Exception:
        log.exception("list_cron_schedule: scheduler import/metadata failed")
        metadata = []

    if not metadata:
        return []

    now = int(time.time())
    cutoff_24h = now - 86400

    try:
        with db.conn() as c:
            agg_rows = c.execute(
                """
                SELECT
                  job_name,
                  MAX(started_at) AS last_run,
                  COUNT(*) AS total_24h,
                  SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_24h,
                  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS fail_24h
                FROM job_runs
                WHERE started_at >= ?
                GROUP BY job_name
                """,
                (cutoff_24h,),
            ).fetchall()
            last_run_rows = c.execute(
                """
                SELECT job_name, MAX(started_at) AS last_run
                FROM job_runs GROUP BY job_name
                """
            ).fetchall()
            last_status_rows = c.execute(
                """
                SELECT r.job_name, r.ok, r.completed_at, r.error
                FROM job_runs r
                INNER JOIN (
                  SELECT job_name, MAX(started_at) AS max_started
                  FROM job_runs GROUP BY job_name
                ) m ON m.job_name = r.job_name AND m.max_started = r.started_at
                """
            ).fetchall()
    except Exception:
        log.exception("list_cron_schedule: aggregation failed")
        agg_rows, last_run_rows, last_status_rows = [], [], []

    agg = {r["job_name"]: dict(r) for r in agg_rows}
    last_run_any = {r["job_name"]: r["last_run"] for r in last_run_rows}
    last_status = {r["job_name"]: _status_from_row(r) for r in last_status_rows}

    out: list[dict] = []
    for meta in metadata:
        name = meta["name"]
        a = agg.get(name, {})
        total = int(a.get("total_24h") or 0)
        ok_count = int(a.get("ok_24h") or 0)
        success_rate = round(100.0 * ok_count / total, 1) if total > 0 else None
        out.append({
            "name": name,
            "schedule": meta.get("trigger") or "",
            "func_module": meta.get("func_module") or "",
            "func_name": meta.get("func_name") or "",
            "next_run": meta.get("next_run_time"),
            "last_run": last_run_any.get(name),
            "last_status": last_status.get(name) or "unknown",
            "success_rate_24h": success_rate,
            "runs_24h": total,
            "fails_24h": int(a.get("fail_24h") or 0),
            "paused": bool(meta.get("paused")),
        })
    return out


def get_job_stats(window_hours: int = 24) -> dict:
    """Top-of-page counters for the stats bar.

    Returns ``{total_runs, success_count, failed_count, success_rate,
    avg_duration_ms, window_hours}``.
    """
    window_seconds = max(1, int(window_hours)) * 3600
    cutoff = int(time.time()) - window_seconds
    try:
        with db.conn() as c:
            row = c.execute(
                """
                SELECT
                  COUNT(*) AS total_runs,
                  SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS success_count,
                  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_count,
                  AVG(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END) AS avg_duration_ms
                FROM job_runs
                WHERE started_at >= ?
                """,
                (cutoff,),
            ).fetchone()
    except Exception:
        log.exception("get_job_stats failed")
        return {
            "total_runs": 0, "success_count": 0, "failed_count": 0,
            "success_rate": None, "avg_duration_ms": None,
            "window_hours": window_hours,
        }
    total = int(row["total_runs"] or 0)
    ok = int(row["success_count"] or 0)
    fail = int(row["failed_count"] or 0)
    avg = row["avg_duration_ms"]
    avg_ms = int(avg) if avg is not None else None
    rate = round(100.0 * ok / total, 1) if total > 0 else None
    return {
        "total_runs": total,
        "success_count": ok,
        "failed_count": fail,
        "success_rate": rate,
        "avg_duration_ms": avg_ms,
        "window_hours": window_hours,
    }


def list_distinct_job_names() -> list[str]:
    """Return every distinct ``job_name`` that has ever recorded a run,
    union'd with currently-registered scheduler jobs. Used to populate
    the filter dropdown."""
    names: set[str] = set()
    try:
        with db.conn() as c:
            for row in c.execute(
                "SELECT DISTINCT job_name FROM job_runs"
            ).fetchall():
                names.add(row["job_name"])
    except Exception:
        log.exception("list_distinct_job_names: query failed")
    try:
        from scheduler import scheduler as sched
        for m in sched.jobs_metadata():
            names.add(m["name"])
    except Exception:
        pass
    return sorted(names)
