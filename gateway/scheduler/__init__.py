"""Centralised APScheduler for every recurring job narve.ai runs.

Public API::

    from scheduler import scheduler
    scheduler.start()
    scheduler.pause("health_check")
    scheduler.trigger_now("weekly_reports")

At startup server.py imports ``scheduler.registry`` which wires every
recurring job. The scheduler writes ``job_runs`` audit rows and the
``/admin/jobs`` page reads them.
"""

from scheduler.scheduler import scheduler, record_start, record_end  # noqa: F401
from scheduler.decorators import scheduled_job  # noqa: F401
