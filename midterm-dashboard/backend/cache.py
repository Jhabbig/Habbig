from __future__ import annotations
"""Tiny in-process caches.

Election-night traffic hammers a small number of read endpoints (the live
dashboard, the comparison table, the RSS feed). Each of those is a pure
function of DB state, so all concurrent requests within a few seconds can
share one computation.

Two pieces here:

1. ``TTLCache`` — get-or-compute helper with a hard TTL and per-key locks
   so a cache miss doesn't trigger N concurrent recomputes. Under load
   (many clients polling /live/dashboard at the same time) we want the
   first caller to compute and everyone else to wait on its result.

2. ``etag_for`` — content-based ETag generator. Used together with
   ``Cache-Control`` so clients can do conditional GETs and we return
   304 instead of re-serializing.

Both are intentionally simple — no Redis, no external deps. For a single
uvicorn worker this is the right shape. If we ever fan out to multiple
workers we can swap TTLCache for a redis-backed version with the same
``get_or_compute`` interface; the call sites won't change.
"""

import asyncio
import hashlib
import json
import time
from typing import Any, Awaitable, Callable


class TTLCache:
    """In-process cache with a hard TTL and per-key recompute locks."""

    def __init__(self, default_ttl: float = 5.0):
        self._default_ttl = default_ttl
        self._store: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def get(self, key: str) -> Any | None:
        """Return the cached value if present and not expired, else None."""
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            return None
        return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        ttl = ttl if ttl is not None else self._default_ttl
        self._store[key] = (time.monotonic() + ttl, value)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    async def get_or_compute(
        self,
        key: str,
        compute: Callable[[], Awaitable[Any]] | Callable[[], Any],
        ttl: float | None = None,
    ) -> Any:
        """Standard "stampede-safe" pattern.

        If the value is cached and fresh, return it.
        Otherwise, take the per-key lock — the first caller computes; every
        other caller wakes up, checks the cache, and gets the result without
        doing the work themselves.
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        async with self._lock_for(key):
            # Re-check under the lock — another caller may have just computed
            cached = self.get(key)
            if cached is not None:
                return cached
            result = compute()
            if asyncio.iscoroutine(result):
                result = await result
            self.set(key, result, ttl=ttl)
            return result


def etag_for(payload: Any) -> str:
    """Stable content-hash ETag for a JSON-serializable payload.

    Sort keys so dict insertion order doesn't change the hash, then
    truncate the hex digest to 16 chars — enough collision-resistance
    for HTTP caching where the consequence of a collision is a stale
    response that the next poll fixes.
    """
    if isinstance(payload, (bytes, str)):
        body = payload.encode("utf-8") if isinstance(payload, str) else payload
    else:
        body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return f'W/"{hashlib.sha256(body).hexdigest()[:16]}"'
