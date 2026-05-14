"""Async cache with Redis backend + in-process fallback.

Design constraints that fall out of the surrounding codebase:

* **Redis is optional.** rate_limiter.py and jobs/backend.py both tolerate the
  absence of Redis — this module does the same. If `REDIS_URL` is unset or
  the connection fails at startup, we transparently fall back to an in-process
  dict. The API is identical either way.
* **Async-first.** Callers are FastAPI handlers, which run in the event loop.
  Sync `redis.from_url` blocks the loop — we use `redis.asyncio` instead.
* **JSON values.** Responses are already JSON-serialisable dicts. `default=str`
  handles the handful of `datetime`/`Decimal`/`sqlite3.Row` edge cases without
  forcing every caller to pre-serialise.
* **Pattern delete matters.** Invalidation after a write hits families of keys
  (`source:*`, `sources:*`, `source_history:*`). Redis has `SCAN MATCH`, the
  in-process store uses `fnmatch` over the dict keys.
* **Stats for the admin panel.** Hit/miss counters track cache effectiveness
  per-process; `/admin/performance` reads them.

Keys are globally prefixed with `CACHE_KEY_PREFIX` so a future breaking change
(schema rename, value format bump) can flip the version and orphan the old
keys without a manual flush.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import time
from threading import Lock
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("cache")


# Bump when the cache envelope changes (serialisation format, key schema).
# Old-generation keys become unreachable and expire on their own TTL.
CACHE_KEY_PREFIX = "narve:cache:v1:"


def _make_key(key: str) -> str:
    """Prefix every user-supplied key. Also used by invalidation helpers."""
    return f"{CACHE_KEY_PREFIX}{key}"


# ── In-process fallback ─────────────────────────────────────────────────────


class _MemoryBackend:
    """Single-process cache. Thread-safe; TTL enforced lazily on read."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, str]] = {}
        self._lock = Lock()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if expires_at and expires_at < time.time():
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds > 0 else 0
        with self._lock:
            self._store[key] = (expires_at, value)

    def delete(self, key: str) -> int:
        with self._lock:
            return 1 if self._store.pop(key, None) is not None else 0

    def delete_pattern(self, pattern: str) -> int:
        # fnmatch mirrors Redis glob semantics closely enough for our keys
        # (no `[abc]`-class matches in use; `*` and `?` behave the same).
        with self._lock:
            victims = [k for k in self._store if fnmatch.fnmatchcase(k, pattern)]
            for k in victims:
                self._store.pop(k, None)
            return len(victims)

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# ── Cache service ───────────────────────────────────────────────────────────


class CacheService:
    """Async wrapper over Redis with an in-process fallback.

    Instantiated once at module import. If `REDIS_URL` is set and reachable,
    `self._redis` holds an `redis.asyncio.Redis` client; otherwise it stays
    None and `self._memory` handles everything.

    `connect()` is called the first time an async method runs (lazy init so
    import-time failures don't break the server). A failed Redis connection
    caches the failure — we don't keep retrying per request.
    """

    def __init__(self) -> None:
        self._redis: Any = None
        self._memory = _MemoryBackend()
        # asyncio.Lock() in Python 3.9 binds to the current event loop at
        # construction time, which fails outside a running loop (e.g. at
        # CacheService() construction during a sync test that has already
        # called asyncio.run() and closed its loop). Build it lazily on
        # first use, when we're guaranteed to be inside an event loop.
        self._connect_lock: Optional[asyncio.Lock] = None
        self._connect_attempted = False
        self._redis_url = os.environ.get("REDIS_URL", "").strip()
        self._hits = 0
        self._misses = 0
        self._errors = 0
        self._sets = 0
        self._deletes = 0
        self._stats_lock = Lock()
        self._enabled = os.environ.get("CACHE_ENABLED", "true").lower() not in (
            "0", "false", "no", "off",
        )

    # ── connection lifecycle ────────────────────────────────────────────

    async def _ensure_connected(self) -> None:
        """Connect to Redis on first use. Idempotent; failure is sticky."""
        if self._connect_attempted or not self._redis_url:
            return
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._connect_attempted:
                return
            self._connect_attempted = True
            try:
                import redis.asyncio as aioredis
                client = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    socket_timeout=1.0,
                    socket_connect_timeout=1.0,
                )
                await client.ping()
                self._redis = client
                log.info(
                    "cache: Redis backend connected (%s)",
                    self._redis_url.split("@")[-1],
                )
            except Exception as exc:
                log.warning(
                    "cache: REDIS_URL set but connection failed (%s); "
                    "using in-process fallback", exc,
                )
                self._redis = None

    async def close(self) -> None:
        """Release the Redis connection. Called from app shutdown."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
        self._connect_attempted = False

    # ── core operations ─────────────────────────────────────────────────

    async def get(self, key: str) -> Any:
        """Return the cached value or None if missing/expired."""
        if not self._enabled:
            return None
        await self._ensure_connected()
        full = _make_key(key)
        raw: Optional[str] = None
        try:
            if self._redis is not None:
                raw = await self._redis.get(full)
            else:
                raw = self._memory.get(full)
        except Exception as exc:
            self._record_error()
            log.warning("cache.get failed for %s: %s", key, exc)
            return None

        if raw is None:
            self._record_miss()
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            # Corrupt entry — invalidate so the next caller repopulates cleanly.
            self._record_error()
            log.warning("cache.get bad JSON for %s: %s", key, exc)
            try:
                await self.delete(key)
            except Exception:
                pass
            return None
        self._record_hit()
        return value

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        """Serialise and store `value`. A ttl <= 0 is clamped to 60s; the
        cache should not grow without bound."""
        if not self._enabled:
            return
        if ttl_seconds <= 0:
            ttl_seconds = 60
        await self._ensure_connected()
        full = _make_key(key)
        try:
            raw = json.dumps(value, default=str)
        except (TypeError, ValueError) as exc:
            # Non-serialisable value: log and skip. Caller gets no cache but
            # the endpoint still works.
            self._record_error()
            log.warning("cache.set skip (unserialisable) for %s: %s", key, exc)
            return
        try:
            if self._redis is not None:
                await self._redis.set(full, raw, ex=ttl_seconds)
            else:
                self._memory.set(full, raw, ttl_seconds)
            with self._stats_lock:
                self._sets += 1
        except Exception as exc:
            self._record_error()
            log.warning("cache.set failed for %s: %s", key, exc)

    async def delete(self, key: str) -> int:
        """Delete a single key. Returns 1 if present, 0 otherwise."""
        await self._ensure_connected()
        full = _make_key(key)
        try:
            if self._redis is not None:
                n = int(await self._redis.delete(full))
            else:
                n = self._memory.delete(full)
            if n:
                with self._stats_lock:
                    self._deletes += n
            return n
        except Exception as exc:
            self._record_error()
            log.warning("cache.delete failed for %s: %s", key, exc)
            return 0

    async def delete_pattern(self, pattern: str) -> int:
        """Delete every key matching `pattern` (glob syntax — `*`, `?`).

        Redis iterates via SCAN in batches of 500; the in-process backend
        walks its dict. Both branches return the total number of keys
        removed.
        """
        await self._ensure_connected()
        full = _make_key(pattern)
        try:
            if self._redis is not None:
                removed = 0
                batch: list[str] = []
                async for key in self._redis.scan_iter(match=full, count=500):
                    batch.append(key)
                    if len(batch) >= 500:
                        removed += int(await self._redis.delete(*batch))
                        batch.clear()
                if batch:
                    removed += int(await self._redis.delete(*batch))
            else:
                removed = self._memory.delete_pattern(full)
            if removed:
                with self._stats_lock:
                    self._deletes += removed
            return removed
        except Exception as exc:
            self._record_error()
            log.warning("cache.delete_pattern failed for %s: %s", pattern, exc)
            return 0

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        ttl_seconds: int,
    ) -> Any:
        """Fetch-through helper: return cached value, or call `factory()`
        and cache its result. Exceptions from `factory` propagate — they
        are the caller's to handle and we don't want to cache error
        responses."""
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await factory()
        # None is a legitimate response body (e.g. "source not found"). Store
        # a sentinel dict instead so we still cache the "no data" answer and
        # stop hammering the DB.
        await self.set(key, value, ttl_seconds)
        return value

    # ── stats for admin panel ───────────────────────────────────────────

    def _record_hit(self) -> None:
        with self._stats_lock:
            self._hits += 1

    def _record_miss(self) -> None:
        with self._stats_lock:
            self._misses += 1

    def _record_error(self) -> None:
        with self._stats_lock:
            self._errors += 1

    def stats(self) -> dict[str, Any]:
        with self._stats_lock:
            hits = self._hits
            misses = self._misses
            errors = self._errors
            sets = self._sets
            deletes = self._deletes
        total = hits + misses
        hit_rate = hits / total if total else 0.0
        return {
            "backend": "redis" if self._redis is not None else "memory",
            "redis_url_configured": bool(self._redis_url),
            "enabled": self._enabled,
            "hits": hits,
            "misses": misses,
            "errors": errors,
            "sets": sets,
            "deletes": deletes,
            "hit_rate": round(hit_rate, 4),
            "memory_size": self._memory.size(),
        }

    def reset_stats(self) -> None:
        """Used by admin panel reset button and by tests."""
        with self._stats_lock:
            self._hits = 0
            self._misses = 0
            self._errors = 0
            self._sets = 0
            self._deletes = 0


# Module-level singleton. Matches the pattern in security/rate_limiter.py.
cache = CacheService()


# ── Invalidation helpers ────────────────────────────────────────────────────
#
# These live next to the cache so callers never have to spell out the key
# glob themselves — the cache owns the key schema. When a new endpoint is
# cached, extend the relevant invalidation helper and every writer is
# updated in one place.


class invalidate:
    """Namespace of invalidation helpers. Use like `invalidate.source("sho")`.

    Each helper is async and returns the number of keys removed (mostly for
    logging and tests — callers usually don't care).
    """

    @staticmethod
    async def source(handle: str) -> int:
        """Invalidate everything scoped to a single source."""
        removed = 0
        removed += await cache.delete(f"source:{handle}")
        removed += await cache.delete(f"source:v1:{handle}")
        removed += await cache.delete(f"source_calibration:{handle}")
        removed += await cache.delete(f"source_profile:{handle}")
        removed += await cache.delete_pattern(f"source_history:{handle}*")
        removed += await cache.delete_pattern(f"credibility:{handle}*")
        return removed

    @staticmethod
    async def all_sources() -> int:
        """After a full credibility recompute: invalidate list/network caches
        and every per-source key. Cheaper than enumerating handles."""
        removed = 0
        removed += await cache.delete_pattern("source:*")
        removed += await cache.delete_pattern("sources:*")
        removed += await cache.delete_pattern("source_calibration:*")
        removed += await cache.delete_pattern("source_history:*")
        removed += await cache.delete_pattern("source_profile:*")
        removed += await cache.delete_pattern("credibility:*")
        removed += await cache.delete_pattern("predictions:*")
        removed += await cache.delete_pattern("market_probability:*")
        return removed

    @staticmethod
    async def market(market_id: str) -> int:
        """After a market resolves or its data changes."""
        removed = 0
        removed += await cache.delete(f"market_probability:{market_id}")
        removed += await cache.delete(f"market_retrospective:{market_id}")
        removed += await cache.delete_pattern(f"market:{market_id}*")
        # A resolved market also changes source scores and prediction lists,
        # so the big-fan-out invalidator runs too.
        removed += await cache.delete_pattern("predictions:*")
        removed += await cache.delete_pattern("env_top:*")
        return removed

    @staticmethod
    async def environmental() -> int:
        """After environmental_impact rows change."""
        return await cache.delete_pattern("env_top:*")

    @staticmethod
    async def everything() -> int:
        """Nuclear option. Used by tests and admin panel debug button."""
        return await cache.delete_pattern("*")
