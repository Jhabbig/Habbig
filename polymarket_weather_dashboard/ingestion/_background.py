"""Background pre-fetch loop.

Spawns a single daemon thread that walks a registered list of (callable,
interval_s) pairs and pre-warms each cached ingestion call on its own
schedule. Means dashboard page loads don't have to wait for a fan-out
across 18 upstreams - the data is already warm in cache.

Each call is wrapped in a try/except so one upstream's failure doesn't
take down the loop.

The schedule is staggered: jobs spread their first call by job-index ×
2s so we don't burst-call 18 endpoints simultaneously on startup.

Disabled by default (so smoke tests don't try to hit live upstreams);
enable by setting ``DISASTERS_PREFETCH=1`` (or ``=true``) in the env.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger("disasters.background")

_started = False
_stop_event = threading.Event()


def _run(jobs: list[tuple[str, Callable[[], object], int]]) -> None:
    last_run: dict[str, float] = {name: 0.0 for name, _, _ in jobs}
    # Stagger initial calls
    for i, (name, fn, interval) in enumerate(jobs):
        delay = i * 2
        threading.Timer(delay, lambda n=name, f=fn: _safe_call(n, f, last_run)).start()
    log.info("Background prefetch loop started with %d jobs", len(jobs))
    while not _stop_event.is_set():
        now = time.time()
        for name, fn, interval in jobs:
            if now - last_run.get(name, 0) >= interval:
                _safe_call(name, fn, last_run)
        _stop_event.wait(15)


def _safe_call(name: str, fn: Callable[[], object], last_run: dict[str, float]) -> None:
    try:
        fn()
        last_run[name] = time.time()
    except Exception as e:  # noqa: BLE001
        log.warning("background %s failed: %s", name, e)
        last_run[name] = time.time()  # don't tight-loop on error


def start(jobs: list[tuple[str, Callable[[], object], int]]) -> None:
    global _started
    if _started:
        return
    if os.environ.get("DISASTERS_PREFETCH", "").lower() not in {"1", "true", "yes"}:
        log.info("Background prefetch disabled (set DISASTERS_PREFETCH=1 to enable)")
        return
    t = threading.Thread(target=_run, args=(jobs,), daemon=True, name="disasters-prefetch")
    t.start()
    _started = True


def stop() -> None:
    _stop_event.set()
