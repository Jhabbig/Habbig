"""Background job queue for narve.ai.

Public API — the rest of the codebase should only import from here:

    from jobs import enqueue_job, enqueue_cron, job_registry
    await enqueue_job("send_email", to="x@y.com", template="welcome", context={})

Backend is chosen at startup:
  - if REDIS_HOST is set and the `arq` package is installed → ARQ/Redis
  - otherwise → InProcessBackend (asyncio tasks, SQLite audit table)

InProcessBackend runs jobs inside the same gateway process. It is the
default for the current single-server SQLite deployment. Swap to ARQ by
setting REDIS_HOST in the environment — no code changes required.
"""

from jobs.registry import job_registry, register_job, register_cron  # noqa: F401
from jobs.backend import (  # noqa: F401
    enqueue_job,
    enqueue_cron,
    start_worker,
    stop_worker,
    get_worker_status,
    list_recent_jobs,
    retry_job,
)

# Import the job modules so their @register_job decorators run at startup.
from jobs import email_jobs  # noqa: F401
from jobs import notification_jobs  # noqa: F401
from jobs import pipeline_jobs  # noqa: F401
from jobs import resolution_jobs  # noqa: F401
from jobs import status_jobs  # noqa: F401
