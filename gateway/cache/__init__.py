"""Application-level response cache.

Separate from the rate-limiter's Redis client (security/rate_limiter.py) and
ARQ's job queue (jobs/backend.py). All three share the same Redis instance
when REDIS_URL is set, but use disjoint key prefixes:

    narve:cache:*   — this module (async, JSON values, TTLs)
    rl:*            — rate limiter (sync, sorted sets)
    arq:*           — ARQ job queue (sync, lists/hashes)

Graceful degradation: if REDIS_URL is unset or the server is unreachable we
fall back to an in-process dict. Single-worker deployments get full caching;
multi-worker deployments only benefit from Redis being present.
"""

from cache.service import CacheService, cache, invalidate
from cache.ttl import TTLCache, ttl_cache, ttl_invalidate, DEFAULT_TTLS

__all__ = [
    # Async Redis-backed cache (existing — primary for cross-request state)
    "CacheService", "cache", "invalidate",
    # Sync in-memory TTL cache (new — hot-path wrappers that must not await)
    "TTLCache", "ttl_cache", "ttl_invalidate", "DEFAULT_TTLS",
]
