"""Background pre-fetch loop. Opt-in via CT_PREFETCH=1."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger("ct.background")

_started = False
_stop_event = threading.Event()


def _run(jobs: list[tuple[str, Callable[[], object], int]]) -> None:
    last_run: dict[str, float] = {name: 0.0 for name, _, _ in jobs}
    for i, (name, fn, _) in enumerate(jobs):
        threading.Timer(i * 2, lambda n=name, f=fn: _safe_call(n, f, last_run)).start()
    log.info("Background prefetch loop started with %d jobs", len(jobs))
    while not _stop_event.is_set():
        now = time.time()
        for name, fn, interval in jobs:
            if now - last_run.get(name, 0) >= interval:
                _safe_call(name, fn, last_run)
        _stop_event.wait(10)


def _safe_call(name: str, fn: Callable[[], object], last_run: dict[str, float]) -> None:
    try:
        fn()
        last_run[name] = time.time()
    except Exception as e:  # noqa: BLE001
        log.warning("background %s failed: %s", name, e)
        last_run[name] = time.time()


def start(jobs: list[tuple[str, Callable[[], object], int]]) -> None:
    global _started
    if _started:
        return
    if os.environ.get("CT_PREFETCH", "").lower() not in {"1", "true", "yes"}:
        log.info("Background prefetch disabled (set CT_PREFETCH=1 to enable)")
        return
    threading.Thread(target=_run, args=(jobs,), daemon=True,
                     name="ct-prefetch").start()
    _started = True


def stop() -> None:
    _stop_event.set()
