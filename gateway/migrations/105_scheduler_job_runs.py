"""Scheduler job-run history.

The new ``scheduler`` package (APScheduler-based) records every recurring-
job execution in ``job_runs``. One row per ``(job_name, started_at)``. The
admin panel reads this table to render last-run / avg-duration /
failed-runs on ``/admin/jobs``.

This is distinct from ``background_jobs`` (see jobs/backend.py), which
logs one-shot enqueued work — emails, pipeline kicks, etc. That table
stays untouched. One module, two tables, two purposes:

  * ``background_jobs`` — enqueued fire-and-forget work
  * ``job_runs``        — scheduled recurring work

Additive, idempotent, no backfill.
"""

from __future__ import annotations

revision = "105"
down_revision = "100"


def upgrade(c):
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS job_runs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            job_name       TEXT NOT NULL,
            started_at     INTEGER NOT NULL,
            completed_at   INTEGER,
            duration_ms    INTEGER,
            ok             INTEGER,
            error          TEXT,
            triggered_by   TEXT NOT NULL DEFAULT 'schedule'
        )
        """
    )
    # Admin dashboard queries "latest N runs for a job" and "last run per
    # job" — both benefit from a composite index on (name, started_at desc).
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_runs_name_time "
        "ON job_runs(job_name, started_at DESC)"
    )
    # Separate index for "failed runs across all jobs" view.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_runs_failures "
        "ON job_runs(ok, started_at DESC) WHERE ok = 0"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_job_runs_failures")
    c.execute("DROP INDEX IF EXISTS idx_job_runs_name_time")
    c.execute("DROP TABLE IF EXISTS job_runs")
