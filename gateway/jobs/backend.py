"""Job queue backend abstraction.

Two implementations share one interface:

1. InProcessBackend — default. Spawns asyncio tasks in the gateway process.
   Persists a job audit log in the `background_jobs` SQLite table so the
   admin panel can show history, status, and retry failed jobs. Supports a
   tiny internal cron scheduler that fires registered cron jobs on the
   minute boundary. Zero external dependencies.

2. ArqBackend — used when REDIS_HOST is set and `arq` is importable. Delegates
   to a real ARQ worker process (`arq jobs.worker.WorkerSettings`). The audit
   log is still written so the admin panel works identically.

The rest of the codebase only calls `enqueue_job(name, **kwargs)`, which
picks the active backend and writes to the audit log.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import time
import traceback
from typing import Any, Optional

import db
from jobs.registry import job_registry, cron_jobs


log = logging.getLogger("jobs")


# ── Audit log helpers ───────────────────────────────────────────────────


def _ensure_jobs_table() -> None:
    """Create the background_jobs audit table if it does not exist.

    Kept here (not in db.py init_db) because the jobs module is import-time
    optional — if Redis is present you might still want the audit log.
    """
    with db.conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS background_jobs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL,
                payload        TEXT,
                status         TEXT NOT NULL DEFAULT 'queued',
                attempts       INTEGER NOT NULL DEFAULT 0,
                max_attempts   INTEGER NOT NULL DEFAULT 3,
                error          TEXT,
                result         TEXT,
                enqueued_at    INTEGER NOT NULL,
                started_at     INTEGER,
                finished_at    INTEGER,
                duration_ms    INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON background_jobs(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_name ON background_jobs(name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_enqueued ON background_jobs(enqueued_at)")


def _audit_insert(name: str, payload: dict, max_attempts: int = 3) -> int:
    _ensure_jobs_table()
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO background_jobs (name, payload, status, max_attempts, enqueued_at) "
            "VALUES (?, ?, 'queued', ?, ?)",
            (name, json.dumps(payload, default=str), max_attempts, int(time.time())),
        )
        return cur.lastrowid


def _audit_start(job_id: int) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE background_jobs SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ?",
            (int(time.time()), job_id),
        )


def _audit_finish(job_id: int, *, result: Any = None, error: Optional[str] = None) -> None:
    with db.conn() as c:
        row = c.execute("SELECT started_at FROM background_jobs WHERE id = ?", (job_id,)).fetchone()
        duration_ms = None
        if row and row["started_at"]:
            duration_ms = int((time.time() - row["started_at"]) * 1000)
        status = "failed" if error else "complete"
        c.execute(
            "UPDATE background_jobs SET status = ?, finished_at = ?, duration_ms = ?, "
            "result = ?, error = ? WHERE id = ?",
            (
                status,
                int(time.time()),
                duration_ms,
                json.dumps(result, default=str) if result is not None else None,
                error,
                job_id,
            ),
        )


def list_recent_jobs(limit: int = 50, status_filter: Optional[str] = None) -> list[dict]:
    _ensure_jobs_table()
    with db.conn() as c:
        if status_filter:
            rows = c.execute(
                "SELECT * FROM background_jobs WHERE status = ? ORDER BY enqueued_at DESC LIMIT ?",
                (status_filter, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM background_jobs ORDER BY enqueued_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("enqueued_at"):
            d["enqueued_at_human"] = _dt.datetime.fromtimestamp(d["enqueued_at"]).strftime("%Y-%m-%d %H:%M:%S")
        out.append(d)
    return out


# ── In-process backend ─────────────────────────────────────────────────────


class InProcessBackend:
    """Runs jobs as asyncio tasks in the gateway process.

    This is the default when Redis is not configured. It is NOT suitable for
    multi-worker deployments — use ArqBackend for that. For a single-server
    SQLite-backed setup it is perfectly adequate and adds zero operational
    burden.
    """

    def __init__(self, max_concurrent: int = 10):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._cron_task: Optional[asyncio.Task] = None
        self._shutdown = asyncio.Event()

    async def enqueue(self, name: str, **kwargs) -> int:
        if name not in job_registry:
            raise ValueError(f"unknown job: {name}")
        job_id = _audit_insert(name, kwargs)
        # Spawn without awaiting — fire-and-forget.
        asyncio.create_task(self._run(job_id, name, kwargs))
        return job_id

    async def _run(self, job_id: int, name: str, kwargs: dict) -> None:
        async with self._sem:
            fn = job_registry.get(name)
            if not fn:
                _audit_finish(job_id, error=f"unregistered job: {name}")
                return
            max_attempts = 3
            attempt = 0
            last_error: Optional[str] = None
            while attempt < max_attempts:
                attempt += 1
                _audit_start(job_id)
                try:
                    result = await asyncio.wait_for(fn(**kwargs), timeout=300)
                    _audit_finish(job_id, result=result)
                    return
                except asyncio.TimeoutError:
                    last_error = "timeout after 300s"
                    log.warning("job %s timed out (attempt %d/%d)", name, attempt, max_attempts)
                except Exception as e:
                    last_error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[:800]}"
                    log.warning("job %s failed (attempt %d/%d): %s", name, attempt, max_attempts, e)
                if attempt < max_attempts:
                    await asyncio.sleep(2 ** attempt)  # exponential backoff
            _audit_finish(job_id, error=last_error)

    async def start(self) -> None:
        _ensure_jobs_table()
        # Default: APScheduler (scheduler/*) drives recurring cron jobs
        # now. Leave one-shot ``enqueue_job`` dispatch active but skip
        # spawning the legacy cron loop — otherwise every job fires twice.
        # Set NARVE_LEGACY_CRON_LOOP=1 to restore the old loop (used for
        # emergency rollback if APScheduler is misbehaving).
        if os.environ.get("NARVE_LEGACY_CRON_LOOP", "").lower() in ("1", "true"):
            if self._cron_task is None:
                self._cron_task = asyncio.create_task(self._cron_loop())
                log.info("jobs: in-process backend started with legacy cron loop (%d cron jobs)", len(cron_jobs))
        else:
            log.info(
                "jobs: in-process backend started (legacy cron loop disabled — APScheduler owns %d cron jobs)",
                len(cron_jobs),
            )

    async def stop(self) -> None:
        self._shutdown.set()
        if self._cron_task:
            self._cron_task.cancel()
            try:
                await self._cron_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cron_task = None

    async def _cron_loop(self) -> None:
        """Fire cron jobs whose schedule matches the current minute."""
        # Align to the next minute boundary first.
        now = _dt.datetime.utcnow()
        sleep_for = 60 - now.second
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_for)
            return
        except asyncio.TimeoutError:
            pass

        while not self._shutdown.is_set():
            now = _dt.datetime.utcnow()
            for job in cron_jobs:
                if job["minute"] is not None and now.minute != job["minute"]:
                    continue
                if job["hour"] is not None and now.hour != job["hour"]:
                    continue
                if job["weekday"] is not None and now.weekday() != job["weekday"]:
                    continue
                if job["day"] is not None and now.day != job["day"]:
                    continue
                try:
                    await self.enqueue(job["name"])
                except Exception as e:
                    log.exception("cron enqueue failed for %s: %s", job["name"], e)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass


# ── ARQ backend (optional) ─────────────────────────────────────────────────


class ArqBackend:
    """Thin wrapper around arq.create_pool that writes the same audit log.

    Used only when REDIS_HOST is set and the arq package is importable.
    The actual job execution happens in a separate `arq` worker process — run
    it alongside the gateway with `arq jobs.worker.WorkerSettings`.
    """

    def __init__(self):
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings
            self._pool = await create_pool(RedisSettings(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                database=int(os.environ.get("REDIS_DB", "0")),
                password=os.environ.get("REDIS_PASSWORD") or None,
            ))
        return self._pool

    async def enqueue(self, name: str, **kwargs) -> int:
        pool = await self._get_pool()
        job_id = _audit_insert(name, kwargs)
        await pool.enqueue_job(name, _audit_job_id=job_id, **kwargs)
        return job_id

    async def start(self) -> None:
        _ensure_jobs_table()
        # The actual worker runs in a separate process — nothing to start here.
        log.info("jobs: ARQ backend ready — run `arq jobs.worker.WorkerSettings` in a separate process")

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


# ── Backend selection ──────────────────────────────────────────────────────


_backend: Optional[object] = None


def _select_backend() -> object:
    global _backend
    if _backend is not None:
        return _backend
    if os.environ.get("REDIS_HOST"):
        try:
            import arq  # noqa: F401
            _backend = ArqBackend()
            log.info("jobs: using ArqBackend (REDIS_HOST set)")
            return _backend
        except ImportError:
            log.warning("jobs: REDIS_HOST set but arq not installed — falling back to in-process")
    _backend = InProcessBackend()
    return _backend


async def enqueue_job(name: str, **kwargs) -> int:
    """Enqueue a job by name. Returns the audit-log row id."""
    backend = _select_backend()
    return await backend.enqueue(name, **kwargs)


async def enqueue_cron() -> None:
    """No-op — cron schedules are evaluated inside the backend. Kept for API symmetry."""
    return None


async def start_worker() -> None:
    backend = _select_backend()
    await backend.start()


async def stop_worker() -> None:
    backend = _select_backend()
    await backend.stop()


def get_worker_status() -> dict:
    backend = _select_backend()
    return {
        "backend": type(backend).__name__,
        "registered_jobs": sorted(job_registry.keys()),
        "cron_jobs": len(cron_jobs),
    }


async def retry_job(job_id: int) -> bool:
    """Re-enqueue a failed job with its original payload."""
    with db.conn() as c:
        row = c.execute("SELECT name, payload FROM background_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return False
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        payload = {}
    await enqueue_job(row["name"], **payload)
    return True
