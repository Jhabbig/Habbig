"""Register every recurring job with the singleton scheduler.

Two sources feed the registry:

1. **Legacy ``jobs/`` cron decorators** — the existing codebase has ~20
   ``register_cron(name, ...)`` calls scattered across ``jobs/*.py``.
   Instead of editing each file, we import the ``jobs`` package (which
   runs every @register_cron at import time, populating the
   ``cron_jobs`` list) and translate each entry into APScheduler. This
   keeps the diff tiny and lets existing job modules keep their shape.

2. **Explicit new-style registrations** — this module also wires the
   recurring jobs the spec calls out by name (health_check,
   market_movement_detect, etc.). Where a legacy job already covers the
   same ground (e.g. ``sync_polymarket_portfolios`` already exists and
   fires every 10 min), we do NOT re-register; the legacy schedule wins.

``register_all()`` is idempotent — calling it twice replaces existing
jobs on APScheduler (``replace_existing=True``). Safe to call on
startup + again after a config change.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

log = logging.getLogger("narve.scheduler.registry")


# ─── Adapter: legacy @register_cron → APScheduler ────────────────────────

def _cron_from_legacy(entry: dict) -> str:
    """Convert the arq-style entry to a 5-field cron string.

    arq semantics:
      * ``None`` means "any" (every tick)
      * ``weekday=0`` is Monday (standard cron: 1 = Monday, 0 = Sunday)
      * ``day=`` is day-of-month
    APScheduler's CronTrigger accepts the same expressions as classic
    crontab (0 = Sunday, 1 = Monday, ...). So we shift ``weekday`` by 1.

    Unset fields become ``*``.
    """
    minute = entry.get("minute")
    hour = entry.get("hour")
    weekday = entry.get("weekday")
    day = entry.get("day")

    def _field(v: Optional[int], is_weekday: bool = False) -> str:
        if v is None:
            return "*"
        if is_weekday:
            # arq 0=Mon -> cron 1=Mon. 6 (Sun) -> 0.
            return str((v + 1) % 7)
        return str(v)

    m = _field(minute)
    h = _field(hour)
    dom = _field(day)
    dow = _field(weekday, is_weekday=True)
    return f"{m} {h} {dom} * {dow}"


def _wrap_legacy_job(job_name: str) -> Callable[[], Any]:
    """Build a coroutine that resolves and runs a legacy job by name.

    Legacy jobs are looked up in ``jobs.registry.job_registry`` at
    fire-time rather than at registration time. That way a reload of
    ``jobs/*.py`` picks up the new function body without rebuilding
    the whole scheduler.
    """
    async def _runner() -> None:
        from jobs.registry import job_registry
        fn = job_registry.get(job_name)
        if fn is None:
            raise RuntimeError(f"legacy job {job_name!r} not in registry")
        await fn()
    _runner.__name__ = f"legacy:{job_name}"
    _runner.__module__ = "scheduler.registry"
    return _runner


# ─── Public API ──────────────────────────────────────────────────────────

def register_all() -> None:
    """Populate the singleton scheduler from both sources."""
    from scheduler.scheduler import scheduler
    from scheduler.decorators import drain_pending

    # 1) Drain any @scheduled_job decorators that have already fired.
    for pending in drain_pending():
        name = pending["name"]
        func = pending["func"]
        if pending["seconds"] is not None:
            scheduler.add_interval(
                name, func, pending["seconds"],
                max_instances=pending["max_instances"],
            )
        else:
            scheduler.add_cron(
                name, func, pending["cron"],
                max_instances=pending["max_instances"],
            )

    # 2) Import the legacy jobs package so its @register_cron calls fire.
    # Wrap in try/except — one bad legacy module must not stop the rest.
    legacy_cron_entries: list[dict] = []
    try:
        import jobs  # noqa: F401 — side-effect import populates registry
        from jobs.registry import cron_jobs, job_registry
        legacy_cron_entries = list(cron_jobs)
        log.info(
            "scheduler: loaded %d legacy cron entries across %d job functions",
            len(legacy_cron_entries), len(job_registry),
        )
    except Exception as exc:
        log.warning("scheduler: legacy jobs import failed: %s", exc)

    # Deduplicate legacy entries: if the same (name, minute, hour, weekday)
    # appears multiple times, only the first wins. The legacy format
    # permits a single job to be scheduled at multiple hours by
    # registering several rows for the same ``name`` — preserve those.
    seen: set[tuple] = set()
    for entry in legacy_cron_entries:
        name = entry["name"]
        key = (
            name,
            entry.get("minute"), entry.get("hour"),
            entry.get("weekday"), entry.get("day"),
        )
        if key in seen:
            continue
        seen.add(key)

        # Build a unique APScheduler id per occurrence. Legacy jobs that
        # register the same name at multiple slots (e.g.
        # fetch_congressional_trades at 00:17/06:17/12:17/18:17) need
        # distinct IDs so they all run. Use a hash-style suffix.
        cron = _cron_from_legacy(entry)
        suffix = cron.replace(" ", "_").replace("*", "x")
        job_id = f"{name}@{suffix}" if any(
            e for e in legacy_cron_entries
            if e["name"] == name and e is not entry
        ) else name

        try:
            scheduler.add_cron(
                job_id, _wrap_legacy_job(name), cron,
            )
        except ValueError as exc:
            log.warning(
                "scheduler: skipping legacy %s (%s): %s",
                name, cron, exc,
            )

    # 3) Explicit new-style recurring jobs the spec calls out. We only
    # add the ones that aren't already covered by a legacy registration.
    _wire_spec_jobs(scheduler, {e["name"] for e in legacy_cron_entries})


def _wire_spec_jobs(sched: Any, covered: set[str]) -> None:
    """Register the spec-named jobs, skipping any that already exist in
    the legacy registry.

    This intentionally uses try-import per job so a missing job module
    leaves the rest functional. During the migration window where some
    of these don't exist yet, they simply no-op.
    """

    def _try(name: str, importer: Callable[[], Callable[..., Any]], spec_fn: Callable[[Callable[..., Any]], None]) -> None:
        if name in covered:
            log.debug("scheduler: %s already covered by legacy registry", name)
            return
        try:
            fn = importer()
        except Exception as exc:
            log.info("scheduler: %s not wired (%s)", name, exc)
            return
        spec_fn(fn)
        log.info("scheduler: %s wired via spec", name)

    # ``check_services`` — not present in legacy; if/when it lands it'll
    # be added here. Skipping silently for now.
    _try(
        "health_check",
        lambda: __import__("jobs.status_jobs", fromlist=["check_service_health"]).check_service_health,
        lambda fn: sched.add_interval("health_check", fn, seconds=60),
    )

    # Daily Claude spend — legacy already covers "check_daily_claude_spend"
    # at 00:05 UTC. The spec wants an additional 09:00 UTC pulse. Register
    # under a distinct id so both fire.
    _try(
        "claude_cost_daily_09utc",
        lambda: __import__("jobs.claude_cost_check", fromlist=["check_daily_claude_spend_job"]).check_daily_claude_spend_job,
        lambda fn: sched.add_cron("claude_cost_daily_09utc", fn, "0 9 * * *"),
    )


__all__ = ["register_all"]
