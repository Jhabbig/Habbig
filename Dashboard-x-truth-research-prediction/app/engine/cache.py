"""Content-hash dedup cache for fusion results.

Many concurrent users request predictions about the same posts/pages; keying
on sha256 of the job *content* (not job_id/user_id) means each unique input is
fused once and everyone else gets the cached result. Backed by Redis when
REDIS_URL is set (shared across workers — the horizontal-scale path), with an
in-process TTL store as the standalone/dev fallback. All operations are
lock-free on the asyncio event loop; there are no per-request global locks.

Hits/misses are counted here and surfaced through the metrics endpoint —
cache hit rate is a first-class cost metric (the upstream Sonnet call is ~85%
of unit cost, so every hit is money).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "engine:fusion:"


class DedupCache:
    def __init__(self, ttl_seconds: int = 900, max_entries: int = 50000,
                 redis_url: Optional[str] = None) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self._store: dict[str, tuple[float, str]] = {}  # key -> (expires_at, payload)
        self._redis = None
        url = redis_url if redis_url is not None else settings.get("REDIS_URL", "")
        if url:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(url, decode_responses=True)
                logger.info("Engine dedup cache using Redis at %s", url.split("@")[-1])
            except Exception as exc:
                logger.warning("Redis unavailable (%s); falling back to in-process cache", exc)
                self._redis = None

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    async def get(self, key: str) -> Optional[dict]:
        payload: Optional[str] = None
        if self._redis is not None:
            try:
                payload = await self._redis.get(_KEY_PREFIX + key)
            except Exception as exc:
                logger.warning("Redis GET failed (%s); serving from memory fallback", exc)
        if payload is None:
            entry = self._store.get(key)
            if entry is not None:
                expires_at, stored = entry
                if expires_at >= time.monotonic():
                    payload = stored
                else:
                    self._store.pop(key, None)
        if payload is None:
            self.misses += 1
            return None
        self.hits += 1
        try:
            return json.loads(payload)
        except ValueError:
            self.misses += 1
            self.hits -= 1
            return None

    async def set(self, key: str, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":"))
        if self._redis is not None:
            try:
                await self._redis.setex(_KEY_PREFIX + key, self.ttl_seconds, payload)
                return
            except Exception as exc:
                logger.warning("Redis SETEX failed (%s); writing to memory fallback", exc)
        self._store[key] = (time.monotonic() + self.ttl_seconds, payload)
        if len(self._store) > self.max_entries:
            self._evict()

    def _evict(self) -> None:
        """Drop expired entries; if still over budget, drop the oldest ~10%."""
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._store.items() if exp < now]
        for k in expired:
            self._store.pop(k, None)
        overflow = len(self._store) - self.max_entries
        if overflow > 0:
            by_expiry = sorted(self._store.items(), key=lambda kv: kv[1][0])
            for k, _ in by_expiry[: max(overflow, len(self._store) // 10)]:
                self._store.pop(k, None)

    def stats(self) -> dict:
        return {
            "backend": self.backend,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hit_rate(), 4),
            "memory_entries": len(self._store),
            "ttl_seconds": self.ttl_seconds,
        }
