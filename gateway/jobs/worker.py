"""ARQ worker entry point.

Run with:

    arq jobs.worker.WorkerSettings

Only used when REDIS_HOST is configured. The gateway process itself falls
back to the in-process backend in dev / single-server setups.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Configure logging BEFORE importing any job modules so every get_logger()
# call inside them picks up the centralised structured handlers. SERVICE_NAME
# defaults to "worker" so BetterStack routing uses LOGTAIL_TOKEN_WORKER.
os.environ.setdefault("SERVICE_NAME", "worker")

import sys as _sys
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

from logging_config import configure_logging, get_logger  # noqa: E402
configure_logging(base_dir=_REPO_ROOT)
log = get_logger("jobs.worker")

# Import all job modules so @register_job decorators fire.
from jobs import email_jobs, notification_jobs, pipeline_jobs  # noqa: F401,E402
from jobs.registry import job_registry, cron_jobs  # noqa: E402


async def _on_startup(ctx: dict) -> None:
    """ARQ lifecycle hook — runs once when the worker process boots."""
    log.info(
        "Worker started",
        extra={
            "registered_jobs": sorted(job_registry.keys()),
            "cron_jobs": len(cron_jobs),
        },
    )


async def _on_shutdown(ctx: dict) -> None:
    """ARQ lifecycle hook — runs once when the worker process exits."""
    log.info("Worker shutting down")


def _as_arq_cron():
    """Convert our simple cron entries into arq.cron objects."""
    try:
        from arq import cron
    except ImportError:
        return []
    entries = []
    for c in cron_jobs:
        fn = job_registry.get(c["name"])
        if not fn:
            continue
        kwargs = {}
        if c["minute"] is not None:
            kwargs["minute"] = c["minute"]
        if c["hour"] is not None:
            kwargs["hour"] = c["hour"]
        if c["weekday"] is not None:
            kwargs["weekday"] = c["weekday"]
        if c["day"] is not None:
            kwargs["day"] = c["day"]
        entries.append(cron(fn, **kwargs))
    return entries


def _redis_settings() -> Any:
    from arq.connections import RedisSettings
    return RedisSettings(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        database=int(os.environ.get("REDIS_DB", "0")),
        password=os.environ.get("REDIS_PASSWORD") or None,
    )


class WorkerSettings:
    """ARQ worker configuration."""
    functions = list(job_registry.values())
    try:
        redis_settings = _redis_settings()
    except Exception:
        redis_settings = None
    max_jobs = 10
    job_timeout = 300
    keep_result = 3600
    retry_jobs = True
    max_tries = 3
    cron_jobs = _as_arq_cron()
    on_startup = _on_startup
    on_shutdown = _on_shutdown
