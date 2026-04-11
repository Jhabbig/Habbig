"""Job registry — maps a string name to a coroutine function.

Jobs register themselves via the @register_job decorator at import time.
The backend looks up functions here when it dequeues a job.

Kept deliberately small so the ARQ and in-process backends can both drive
it with identical semantics.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable


_Fn = Callable[..., Awaitable[Any]]
job_registry: dict[str, _Fn] = {}
cron_jobs: list[dict] = []


def register_job(name: str) -> Callable[[_Fn], _Fn]:
    """Decorator. Registers a coroutine under *name* in the global registry."""
    def deco(fn: _Fn) -> _Fn:
        if name in job_registry:
            raise ValueError(f"job already registered: {name}")
        job_registry[name] = fn
        return fn
    return deco


def register_cron(
    name: str,
    *,
    minute: int | None = None,
    hour: int | None = None,
    weekday: int | None = None,
    day: int | None = None,
) -> None:
    """Register a cron schedule for a previously-registered job.

    Semantics match arq.cron. `weekday=0` is Monday. `None` means any.
    """
    cron_jobs.append({
        "name": name,
        "minute": minute,
        "hour": hour,
        "weekday": weekday,
        "day": day,
    })
