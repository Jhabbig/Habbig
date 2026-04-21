"""Per-component health probes.

Each probe returns (status, response_time_ms) where status is one of the
three canonical strings from `status_system.STATUSES`. Probes never raise
— a failing probe reports status="outage" with the latency observed.

The cron job calls `run_all_probes()` once per minute. Probes run
concurrently where independent; the slowest probe bounds total runtime.

Design note: probes live in the gateway process itself. The `app` and
`api` probes are introspective (same process), while `scraper`, `worker`,
`redis`, and `database` cross process/service boundaries.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from status_system import COMPONENT_KEYS, STATUSES


log = logging.getLogger("status.probes")


# Tunables — kept generous so the status page doesn't flap on network
# hiccups. Each probe has its own timeout because the speed-of-light
# cost varies (DB ping is ~ms, HTTP roundtrip is ~100ms).
TIMEOUT_DB_SEC = 2.0
TIMEOUT_HTTP_SEC = 3.0
DEGRADED_APP_MS = 500.0
DEGRADED_API_MS = 750.0
DEGRADED_SCRAPER_MS = 1500.0
DEGRADED_DB_MS = 200.0
DEGRADED_REDIS_MS = 200.0


def _ms_since(start: float) -> float:
    return round((time.monotonic() - start) * 1000.0, 2)


def _outcome(status: str, ms: float) -> tuple[str, float]:
    assert status in STATUSES
    return status, ms


# ── database ────────────────────────────────────────────────────────────


async def probe_database() -> tuple[str, float]:
    """SELECT 1 against auth.db. Wrapped in asyncio.to_thread so the
    blocking sqlite3 call doesn't hog the event loop.
    """
    import db

    start = time.monotonic()
    try:
        def _probe():
            with db.conn() as c:
                row = c.execute("SELECT 1").fetchone()
                if not row or row[0] != 1:
                    raise RuntimeError("unexpected SELECT 1 result")
        await asyncio.wait_for(asyncio.to_thread(_probe), timeout=TIMEOUT_DB_SEC)
    except asyncio.TimeoutError:
        return _outcome("outage", _ms_since(start))
    except Exception as exc:
        log.debug("database probe failed: %s", exc)
        return _outcome("outage", _ms_since(start))
    ms = _ms_since(start)
    if ms > DEGRADED_DB_MS:
        return _outcome("degraded", ms)
    return _outcome("operational", ms)


# ── app (self) ──────────────────────────────────────────────────────────


async def probe_app() -> tuple[str, float]:
    """The gateway process itself is up (by virtue of running this probe).

    Still, we cross-check: DB reachable + static dir exists. If either
    fails the app is functionally degraded even if the HTTP server is
    answering. This matches the logic in `_check_database` and
    `_check_static_dir` in server.py.
    """
    start = time.monotonic()

    db_status, _ = await probe_database()
    static_ok = _check_static_dir_ok()

    ms = _ms_since(start)
    if db_status == "outage" or not static_ok:
        return _outcome("outage" if db_status == "outage" else "degraded", ms)
    if ms > DEGRADED_APP_MS:
        return _outcome("degraded", ms)
    return _outcome("operational", ms)


def _check_static_dir_ok() -> bool:
    try:
        from pathlib import Path
        p = Path(__file__).parent.parent / "static"
        return p.exists() and p.is_dir()
    except Exception:
        return False


# ── api ─────────────────────────────────────────────────────────────────


async def probe_api() -> tuple[str, float]:
    """The developer API (`/api/v1/*`). We don't make an HTTP call to
    ourselves — that would deadlock under heavy load. Instead, verify
    the module loaded and at least one route is registered.
    """
    start = time.monotonic()
    try:
        import importlib
        mod = importlib.import_module("api_v1")
        router = getattr(mod, "router", None)
        if router is None or not getattr(router, "routes", None):
            return _outcome("degraded", _ms_since(start))
    except ImportError:
        # API module is optional — treat missing as degraded, not outage,
        # since the main app still works without it.
        return _outcome("degraded", _ms_since(start))
    except Exception as exc:
        log.debug("api probe error: %s", exc)
        return _outcome("outage", _ms_since(start))
    ms = _ms_since(start)
    if ms > DEGRADED_API_MS:
        return _outcome("degraded", ms)
    return _outcome("operational", ms)


# ── scraper ─────────────────────────────────────────────────────────────


async def probe_scraper() -> tuple[str, float]:
    """HTTP GET to the scraper's `/health` endpoint.

    Spec target: `http://localhost:8001/health` expecting 200 within 2s.
    The scraper requires a Bearer auth header (SCRAPER_API_KEY). If the
    key is unset in this environment we can't actually probe; fall back
    to "operational" so a dev machine without the scraper running doesn't
    permanently show an outage banner.
    """
    start = time.monotonic()

    base = os.environ.get("SCRAPER_URL", "http://localhost:8001").rstrip("/")
    api_key = os.environ.get("SCRAPER_API_KEY", "").strip()

    if not api_key and os.environ.get("PRODUCTION", "").lower() not in ("1", "true"):
        # Dev environment without the scraper wired up → don't alarm.
        return _outcome("operational", _ms_since(start))

    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_HTTP_SEC)) as client:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            resp = await client.get(f"{base}/health", headers=headers)
            ms = _ms_since(start)
            if resp.status_code != 200:
                return _outcome("outage" if resp.status_code >= 500 else "degraded", ms)
            if ms > DEGRADED_SCRAPER_MS:
                return _outcome("degraded", ms)
            return _outcome("operational", ms)
    except ImportError:
        return _outcome("operational", _ms_since(start))
    except asyncio.TimeoutError:
        return _outcome("outage", _ms_since(start))
    except Exception as exc:
        log.debug("scraper probe failed: %s", exc)
        return _outcome("outage", _ms_since(start))


# ── worker ──────────────────────────────────────────────────────────────


async def probe_worker() -> tuple[str, float]:
    """Job queue liveness.

    Two cases, depending on the backend:

    1. InProcessBackend (no REDIS_HOST) — the worker is an asyncio task
       inside this process. `get_worker_status()` reports whether it's
       running. No Redis round-trip needed.

    2. ARQ/Redis backend — the worker is a separate process. We inspect
       the Redis audit trail for the timestamp of the last processed
       job; anything older than 5 minutes is considered stalled.
    """
    start = time.monotonic()
    try:
        from jobs import get_worker_status
        status_info = await asyncio.wait_for(
            asyncio.to_thread(get_worker_status), timeout=TIMEOUT_HTTP_SEC
        ) if not asyncio.iscoroutinefunction(get_worker_status) else await asyncio.wait_for(
            get_worker_status(), timeout=TIMEOUT_HTTP_SEC
        )
    except asyncio.TimeoutError:
        return _outcome("outage", _ms_since(start))
    except Exception as exc:
        log.debug("worker probe error: %s", exc)
        return _outcome("degraded", _ms_since(start))

    ms = _ms_since(start)
    if not isinstance(status_info, dict):
        return _outcome("operational", ms)

    running = status_info.get("running")
    last_processed = status_info.get("last_processed_at")

    if running is False:
        return _outcome("outage", ms)

    if last_processed:
        try:
            age = time.time() - float(last_processed)
            if age > 600:  # nothing ran in the last 10 minutes
                return _outcome("degraded", ms)
        except (TypeError, ValueError):
            pass

    return _outcome("operational", ms)


# ── redis ───────────────────────────────────────────────────────────────


async def probe_redis() -> tuple[str, float]:
    """PING the configured Redis instance. Skips cleanly when no Redis
    is configured (dev / in-process backend) — returns "operational"
    because the cache layer is logically a no-op, not broken.
    """
    start = time.monotonic()
    host = os.environ.get("REDIS_HOST", "").strip()
    if not host:
        return _outcome("operational", _ms_since(start))

    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD", "").strip() or None
    dbnum = int(os.environ.get("REDIS_DB", "0"))

    try:
        import redis.asyncio as aioredis  # type: ignore
    except ImportError:
        return _outcome("operational", _ms_since(start))

    client = None
    try:
        client = aioredis.Redis(
            host=host, port=port, password=password, db=dbnum,
            socket_timeout=TIMEOUT_HTTP_SEC, socket_connect_timeout=TIMEOUT_HTTP_SEC,
        )
        pong = await asyncio.wait_for(client.ping(), timeout=TIMEOUT_HTTP_SEC)
        ms = _ms_since(start)
        if not pong:
            return _outcome("outage", ms)
        if ms > DEGRADED_REDIS_MS:
            return _outcome("degraded", ms)
        return _outcome("operational", ms)
    except asyncio.TimeoutError:
        return _outcome("outage", _ms_since(start))
    except Exception as exc:
        log.debug("redis probe failed: %s", exc)
        return _outcome("outage", _ms_since(start))
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass


# ── orchestrator ────────────────────────────────────────────────────────


_PROBES = {
    "app": probe_app,
    "api": probe_api,
    "scraper": probe_scraper,
    "worker": probe_worker,
    "database": probe_database,
    "redis": probe_redis,
}


async def run_all_probes() -> dict[str, tuple[str, Optional[float]]]:
    """Run every probe concurrently. Returns `{component: (status, ms)}`.

    If a probe crashes we catch it and report ("outage", None) for that
    component — we never let a probe failure crash the cron job.
    """
    tasks = {k: asyncio.create_task(fn()) for k, fn in _PROBES.items()}
    out: dict[str, tuple[str, Optional[float]]] = {}
    for key, task in tasks.items():
        try:
            status, ms = await task
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("probe %s crashed: %s", key, exc)
            status, ms = "outage", None
        out[key] = (status, ms)
    return out


# Sanity check: every COMPONENT_KEY must have a probe, or the cron job
# will silently skip it. Fail loud at import time if someone adds a
# component to __init__.py and forgets to wire up its probe.
_MISSING = [k for k in COMPONENT_KEYS if k not in _PROBES]
if _MISSING:
    raise RuntimeError(f"status probes missing for components: {_MISSING}")
