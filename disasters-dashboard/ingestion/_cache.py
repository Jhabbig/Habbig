"""Tiny in-process TTL cache shared across ingestion modules.

Keeps every module from re-implementing the same locking + TTL logic and
keeps repeated dashboard loads from hammering upstream APIs (USGS, NHC, NWS,
EONET, Polymarket).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

_LOCK = threading.Lock()
_STORE: "OrderedDict[str, dict]" = OrderedDict()
_MAX_KEYS = 128


def get(key: str, ttl_s: int) -> Optional[Any]:
    with _LOCK:
        entry = _STORE.get(key)
        if not entry:
            return None
        if time.time() - entry["t"] > ttl_s:
            _STORE.pop(key, None)
            return None
        _STORE.move_to_end(key)
        return entry["data"]


def put(key: str, data: Any) -> None:
    with _LOCK:
        _STORE[key] = {"t": time.time(), "data": data}
        while len(_STORE) > _MAX_KEYS:
            _STORE.popitem(last=False)


def cached(key: str, ttl_s: int, producer: Callable[[], Any]) -> Any:
    hit = get(key, ttl_s)
    if hit is not None:
        return hit
    data = producer()
    if data is not None:
        put(key, data)
    return data
