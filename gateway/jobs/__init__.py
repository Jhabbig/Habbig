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
from jobs import embed_jobs  # noqa: F401
from jobs import notification_jobs  # noqa: F401
from jobs import pipeline_jobs  # noqa: F401
from jobs import resolution_jobs  # noqa: F401
from jobs import status_jobs  # noqa: F401

# Portfolio sync (Polymarket every 10 min, Kalshi every 15 min),
# subscription reconciliation (daily 03:17 UTC), and Telegram outbound
# jobs. Defensively imported — one bad optional module must not prevent
# the rest of the registry from loading.
try:
    from jobs import sync_portfolios  # noqa: F401
except Exception as _e:  # pragma: no cover
    import logging as _l
    _l.getLogger("jobs").warning("sync_portfolios import failed: %s", _e)

try:
    from jobs import reconcile_subscriptions  # noqa: F401
except Exception as _e:  # pragma: no cover
    import logging as _l
    _l.getLogger("jobs").warning("reconcile_subscriptions import failed: %s", _e)

try:
    from jobs import telegram_sends  # noqa: F401
except Exception as _e:  # pragma: no cover
    import logging as _l
    _l.getLogger("jobs").warning("telegram_sends import failed: %s", _e)

# Intelligence-layer jobs. Each one is self-registering through the
# module-level @register_job / register_cron calls, so just importing
# them is enough. Defensive imports keep a partial schema tree bootable.
for _mod in (
    "claude_cost_check",
    "compute_source_relationships",
    "movement_jobs",
    "generate_weekly_reports",
    "insider_jobs",
    "backtest_jobs",
    "ai_maintenance",
    # Churn/engagement: recomputes churn_signals nightly from the
    # engagement_events ring buffer. Depends on migrations 092+093.
    "compute_churn_signals",
):
    try:
        __import__(f"jobs.{_mod}")
    except Exception as _e:  # pragma: no cover
        import logging as _l
        _l.getLogger("jobs").warning("%s import failed: %s", _mod, _e)
