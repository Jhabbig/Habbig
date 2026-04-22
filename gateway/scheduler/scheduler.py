"""APScheduler wrapper with SQLite audit trail.

Every job registered through this module gets:

1. **Wrapped in a run-recorder.** Each firing writes a `job_runs` row with
   ``started_at``, ``completed_at``, ``duration_ms``, ``ok``, ``error``.
   The admin UI at ``/admin/jobs`` reads that table for last-run /
   avg-duration / failed-runs views.
2. **Crash-isolated.** If a job raises, the exception is logged and
   written to the audit row but never propagated — one broken job can't
   take down the scheduler or the process.
3. **Leader-elected** (soft). ``NARVE_SCHEDULER_LEADER`` env var gates
   starting APScheduler at all. Set to ``"1"`` on exactly one uvicorn
   instance; every other worker returns early from ``start()``. See
   ``RUNBOOK.md`` for the deployment story.

Triggers supported:
  * ``add_interval(name, func, seconds=N)`` — fires every N seconds.
  * ``add_cron(name, func, "m h dom mon dow")`` — classic 5-field cron.

The wrapper also stores ``self.jobs[name]`` with a small dict of trigger
+ target function name, so the admin UI can render its registry without
poking APScheduler internals.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import logging
import os
import time
from typing import Any, Callable, Optional

log = logging.getLogger("narve.scheduler")


def _now() -> int:
    return int(time.time())


# ── Audit log (SQLite) ───────────────────────────────────────────────────
# The migration 105 creates ``job_runs``. We do NOT import db at module
# load time — that would make the scheduler un-importable during test
# collection before the DB is wired up. Pull ``db`` lazily inside each
# helper.

def record_start(job_name: str, triggered_by: str = "schedule") -> int:
    """Insert a ``job_runs`` row marking a run as started. Returns id."""
    import db
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO job_runs (job_name, started_at, triggered_by) "
            "VALUES (?, ?, ?)",
            (job_name, _now(), triggered_by),
        )
        return cur.lastrowid


def record_end(
    run_id: int,
    *,
    ok: bool,
    error: Optional[str] = None,
    started_at: Optional[int] = None,
) -> None:
    """Complete a ``job_runs`` row with duration + outcome."""
    import db
    completed = _now()
    duration_ms: Optional[int] = None
    if started_at is not None:
        duration_ms = max(0, (completed - started_at) * 1000)
    else:
        # Fall back to a DB lookup if the caller didn't carry start time.
        with db.conn() as c:
            row = c.execute(
                "SELECT started_at FROM job_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row:
                duration_ms = max(0, (completed - row["started_at"]) * 1000)

    with db.conn() as c:
        c.execute(
            "UPDATE job_runs SET completed_at = ?, duration_ms = ?, ok = ?, "
            "error = ? WHERE id = ?",
            (completed, duration_ms, 1 if ok else 0, (error or "")[:2000], run_id),
        )


# ── Scheduler wrapper ────────────────────────────────────────────────────

class Scheduler:
    """Thin async-first APScheduler wrapper.

    Kept minimal on purpose: the rest of the app only needs ``start``,
    ``shutdown``, ``add_interval``, ``add_cron``, plus the admin-UI
    helpers (``pause``, ``resume``, ``trigger_now``, ``jobs_metadata``).
    """

    def __init__(self) -> None:
        # Import APScheduler lazily so the scheduler module can be imported
        # from tests even if apscheduler isn't installed. Tests that don't
        # exercise scheduling get the registry/decorators without touching
        # the trigger backends.
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        self._impl = AsyncIOScheduler(timezone="UTC")
        # Registry metadata — persisted across restarts via re-registration
        # at startup. ``trigger`` is a human-readable string for the UI;
        # ``func_module``/``func_name`` identify the target function.
        self.jobs: dict[str, dict[str, Any]] = {}
        # One-shot "next run's triggered_by tag" — popped by the wrapped
        # function the first time it fires after ``trigger_now`` was
        # called. Keyed by job name.
        self._pending_trigger_reasons: dict[str, str] = {}
        self._started = False

    # ── Registration ─────────────────────────────────────────────────
    def add_interval(
        self,
        name: str,
        func: Callable[..., Any],
        seconds: int,
        *,
        max_instances: int = 1,
        **kwargs: Any,
    ) -> None:
        """Fire *func* every *seconds* seconds."""
        from apscheduler.triggers.interval import IntervalTrigger
        self._register(
            name, func, IntervalTrigger(seconds=seconds),
            max_instances=max_instances, **kwargs,
        )

    def add_cron(
        self,
        name: str,
        func: Callable[..., Any],
        cron: str,
        *,
        max_instances: int = 1,
        **kwargs: Any,
    ) -> None:
        """Fire *func* on the schedule described by a classic 5-field
        cron expression: ``"m h dom mon dow"`` (all in UTC).

        Example::
            scheduler.add_cron("weekly_reports", fn, "0 7 * * 1")
        """
        from apscheduler.triggers.cron import CronTrigger
        parts = cron.split()
        if len(parts) != 5:
            raise ValueError(
                f"cron {cron!r} must have 5 whitespace-separated fields "
                "(minute hour day-of-month month day-of-week)"
            )
        m, h, dom, mon, dow = parts
        trigger = CronTrigger(
            minute=m, hour=h, day=dom, month=mon, day_of_week=dow,
            timezone="UTC",
        )
        self._register(name, func, trigger, max_instances=max_instances, **kwargs)

    def _register(
        self,
        name: str,
        func: Callable[..., Any],
        trigger: Any,
        **job_kwargs: Any,
    ) -> None:
        """Wrap *func* with run-recording + crash isolation and bind to APScheduler."""
        is_coro = inspect.iscoroutinefunction(func)

        async def wrapped(_name: str = name, _fn: Any = func, _is_coro: bool = is_coro) -> None:
            started = _now()
            triggered_by = self._pending_trigger_reasons.pop(_name, "schedule")
            run_id: Optional[int] = None
            try:
                run_id = record_start(_name, triggered_by=triggered_by)
            except Exception:
                # Audit failure must not block the job itself.
                log.exception("scheduler: record_start failed for %s", _name)

            try:
                if _is_coro:
                    await _fn()
                else:
                    # Run sync jobs in the default executor so a slow
                    # job can't block the event loop for other jobs.
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, _fn)
                if run_id is not None:
                    try:
                        record_end(run_id, ok=True, started_at=started)
                    except Exception:
                        log.exception("scheduler: record_end(ok) failed for %s", _name)
            except Exception as exc:
                log.exception("scheduler: job %s raised", _name)
                if run_id is not None:
                    try:
                        record_end(run_id, ok=False, error=str(exc), started_at=started)
                    except Exception:
                        log.exception("scheduler: record_end(fail) failed for %s", _name)
                # Swallow — APScheduler would log + surface anyway, but
                # we explicitly don't re-raise so the scheduler process
                # keeps ticking.

        # Allow re-registration so hot-reload during dev doesn't leave
        # stale duplicates. APScheduler's ``replace_existing`` is the
        # supported way to do this.
        self._impl.add_job(
            wrapped, trigger, id=name, replace_existing=True,
            name=name, **job_kwargs,
        )

        # Stash metadata for the admin UI.
        self.jobs[name] = {
            "trigger": str(trigger),
            "func_module": getattr(func, "__module__", "?"),
            "func_name": getattr(func, "__name__", "?"),
            "max_instances": job_kwargs.get("max_instances", 1),
        }

    # ── Lifecycle ────────────────────────────────────────────────────
    def start(self) -> None:
        """Start APScheduler if this process is the elected leader.

        Leader election is deliberately primitive: check
        ``NARVE_SCHEDULER_LEADER=1``. With a single uvicorn process this
        is trivially true; with workers > 1 you MUST set it on exactly
        one. See RUNBOOK.md for why.
        """
        if self._started:
            return
        leader = os.environ.get("NARVE_SCHEDULER_LEADER", "").strip()
        # Default behaviour: in a single-process boot (PRODUCTION=1 or
        # dev), run the scheduler unless explicitly disabled. Multi-
        # worker deploys must opt-in via NARVE_SCHEDULER_LEADER=1 on
        # exactly one worker and unset / "0" on the others.
        if os.environ.get("NARVE_SKIP_SCHEDULER", "").lower() in ("1", "true"):
            log.info("scheduler: NARVE_SKIP_SCHEDULER set — not starting")
            return
        if leader == "0":
            log.info("scheduler: this worker is not the leader — not starting")
            return

        self._impl.start()
        self._started = True
        log.info(
            "scheduler: started with %d jobs (leader=%s)",
            len(self.jobs), leader or "implicit",
        )

    def shutdown(self, wait: bool = False) -> None:
        if not self._started:
            return
        try:
            self._impl.shutdown(wait=wait)
        except Exception:
            log.exception("scheduler: shutdown error")
        finally:
            self._started = False

    # ── Admin helpers ────────────────────────────────────────────────
    def pause(self, name: str) -> None:
        self._impl.pause_job(name)

    def resume(self, name: str) -> None:
        self._impl.resume_job(name)

    def trigger_now(self, name: str, triggered_by: str = "admin") -> None:
        """Fire a job on the next event-loop tick.

        ``triggered_by`` is stashed for the next firing only — the
        wrapped function pops it when it runs, so the audit row shows
        "admin"/"test"/etc. instead of the default "schedule".
        """
        self._pending_trigger_reasons[name] = triggered_by
        self._impl.modify_job(name, next_run_time=_dt.datetime.now(_dt.timezone.utc))

    def jobs_metadata(self) -> list[dict[str, Any]]:
        """Return a list of dicts describing every registered job,
        ordered by name. Used by ``/admin/jobs``.
        """
        rows: list[dict[str, Any]] = []
        for name in sorted(self.jobs.keys()):
            meta = dict(self.jobs[name])
            meta["name"] = name
            aps_job = self._impl.get_job(name) if self._started else None
            meta["next_run_time"] = (
                int(aps_job.next_run_time.timestamp())
                if aps_job and aps_job.next_run_time else None
            )
            meta["paused"] = (
                aps_job.next_run_time is None if aps_job else False
            )
            rows.append(meta)
        return rows


# Singleton used by the registry + admin UI.
scheduler = Scheduler()
