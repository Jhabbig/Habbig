"""``@scheduled_job`` decorator — syntactic sugar over the registry.

Usage::

    from scheduler.decorators import scheduled_job

    @scheduled_job(cron="0 7 * * 1", name="weekly_reports")
    async def send_weekly_digest() -> None:
        ...

    @scheduled_job(seconds=60, name="health_check")
    async def check_services() -> None:
        ...

Exactly one of ``seconds=`` / ``cron=`` must be given. If ``name`` is
omitted, the function's ``__name__`` is used.

The decorator is inert — it only appends to the pending registration
list. ``scheduler.registry.register_all()`` drains that list into the
singleton scheduler at startup.
"""

from __future__ import annotations

from typing import Any, Callable, Optional


_pending: list[dict[str, Any]] = []


def scheduled_job(
    *,
    seconds: Optional[int] = None,
    cron: Optional[str] = None,
    name: Optional[str] = None,
    max_instances: int = 1,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    if (seconds is None) == (cron is None):
        raise ValueError("Pass exactly one of seconds= or cron=")

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _pending.append(
            {
                "name": name or fn.__name__,
                "func": fn,
                "seconds": seconds,
                "cron": cron,
                "max_instances": max_instances,
            }
        )
        return fn

    return decorator


def drain_pending() -> list[dict[str, Any]]:
    """Return and clear the pending list. Called by the registry."""
    out = list(_pending)
    _pending.clear()
    return out
