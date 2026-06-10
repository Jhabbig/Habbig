"""Tiny in-process TTL cache shared across ingestion modules.

Now layered on top of ``_persistence`` so that on a cache miss we first try
to read a fresh-enough value from disk, and on every successful write we
also persist to disk. That makes restarts transparent: YTD counts, climo
priors, and last-known-good projections survive a process bounce and the
dashboard returns immediately on cold start.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from . import _persistence

_LOCK = threading.Lock()
_STORE: "OrderedDict[str, dict]" = OrderedDict()
_MAX_KEYS = 256


def get(key: str, ttl_s: int) -> Optional[Any]:
    with _LOCK:
        entry = _STORE.get(key)
        if entry:
            if time.time() - entry["t"] > ttl_s:
                _STORE.pop(key, None)
            else:
                _STORE.move_to_end(key)
                return entry["data"]
    # Cold path: try disk
    persisted = _persistence.load(key, ttl_s)
    if persisted is not None:
        with _LOCK:
            _STORE[key] = {"t": time.time(), "data": persisted}
        return persisted
    return None


def put(key: str, data: Any) -> None:
    with _LOCK:
        _STORE[key] = {"t": time.time(), "data": data}
        while len(_STORE) > _MAX_KEYS:
            _STORE.popitem(last=False)
    _persistence.store(key, data)


def cached(key: str, ttl_s: int, producer: Callable[[], Any]) -> Any:
    hit = get(key, ttl_s)
    if hit is not None:
        return hit
    data = producer()
    if data is not None:
        put(key, data)
    return data
