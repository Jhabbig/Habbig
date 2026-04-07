"""
Redis caching layer for the gateway.

Provides:
- Key-value caching with TTL for API responses
- Pub/sub for real-time data-change notifications (drives SSE)
- Dashboard-specific cache namespacing

Usage from gateway:
    from cache import cache
    cache.get_api("sports", "/api/data")
    cache.set_api("sports", "/api/data", json_bytes, ttl=30)
    cache.publish("sports", "data_updated")

Usage from dashboard backends (optional — publish when fresh data arrives):
    import redis
    r = redis.Redis()
    r.publish("dashboard:sports", json.dumps({"event": "data_updated"}))
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import redis

log = logging.getLogger("gateway.cache")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Default TTLs per dashboard (seconds).  Dashboards that poll external APIs
# every 300 s get a 60 s cache (always fresh between polls).  Dashboards with
# faster data get shorter TTLs.
DEFAULT_TTLS: dict[str, int] = {
    "sports": 30,
    "weather": 60,
    "world": 30,
    "crypto": 15,
    "midterm": 60,
    "top_traders": 15,
    "stock": 120,
}

FALLBACK_TTL = 30  # seconds


class DashboardCache:
    """Thin wrapper around Redis for gateway caching + pub/sub."""

    def __init__(self, url: str = REDIS_URL):
        self._url = url
        self._r: Optional[redis.Redis] = None
        self._available = False

    # ── Connection ───────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Try to connect to Redis. Returns True if successful."""
        try:
            self._r = redis.Redis.from_url(
                self._url,
                decode_responses=False,  # we store raw bytes for API bodies
                socket_connect_timeout=2,
                socket_timeout=1,
                retry_on_timeout=True,
            )
            self._r.ping()
            self._available = True
            log.info("Redis connected at %s", self._url)
            return True
        except (redis.ConnectionError, redis.TimeoutError) as e:
            log.warning("Redis unavailable (%s) — caching disabled, SSE won't fire", e)
            self._available = False
            return False

    @property
    def available(self) -> bool:
        return self._available and self._r is not None

    # ── API response cache ───────────────────────────────────────────────

    def _api_key(self, dashboard: str, path: str) -> str:
        return f"api:{dashboard}:{path}"

    def get_api(self, dashboard: str, path: str) -> Optional[tuple[bytes, str]]:
        """Return (body_bytes, content_type) or None if miss/expired."""
        if not self.available:
            return None
        try:
            key = self._api_key(dashboard, path)
            data = self._r.get(key)
            if data is None:
                return None
            meta = self._r.get(key + ":meta")
            content_type = meta.decode() if meta else "application/json"
            return data, content_type
        except redis.RedisError as e:
            log.debug("Cache get error: %s", e)
            return None

    def set_api(
        self,
        dashboard: str,
        path: str,
        body: bytes,
        content_type: str = "application/json",
        ttl: Optional[int] = None,
    ) -> None:
        """Cache an API response body with TTL."""
        if not self.available:
            return
        if ttl is None:
            ttl = DEFAULT_TTLS.get(dashboard, FALLBACK_TTL)
        try:
            key = self._api_key(dashboard, path)
            pipe = self._r.pipeline()
            pipe.setex(key, ttl, body)
            pipe.setex(key + ":meta", ttl, content_type.encode())
            pipe.execute()
        except redis.RedisError as e:
            log.debug("Cache set error: %s", e)

    def invalidate(self, dashboard: str, path: Optional[str] = None) -> int:
        """Invalidate one path or all cached paths for a dashboard."""
        if not self.available:
            return 0
        try:
            if path:
                key = self._api_key(dashboard, path)
                return self._r.delete(key, key + ":meta")
            # Wildcard: delete all keys for this dashboard
            pattern = f"api:{dashboard}:*"
            keys = list(self._r.scan_iter(pattern, count=200))
            if keys:
                return self._r.delete(*keys)
            return 0
        except redis.RedisError as e:
            log.debug("Cache invalidate error: %s", e)
            return 0

    # ── Pub/sub (dashboard → gateway → SSE clients) ─────────────────────

    def publish(self, dashboard: str, event: str = "data_updated", data: Optional[dict] = None) -> None:
        """Publish a real-time event for a dashboard."""
        if not self.available:
            return
        try:
            msg = json.dumps({
                "event": event,
                "dashboard": dashboard,
                "ts": time.time(),
                "data": data or {},
            })
            self._r.publish(f"dashboard:{dashboard}", msg)
        except redis.RedisError as e:
            log.debug("Publish error: %s", e)

    def pubsub(self) -> Optional[redis.client.PubSub]:
        """Return a PubSub instance for subscribing. Caller manages the lifecycle."""
        if not self.available:
            return None
        return self._r.pubsub()

    # ── Stats ────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Basic cache stats for the admin panel."""
        if not self.available:
            return {"available": False}
        try:
            info = self._r.info("keyspace")
            db_info = info.get("db0", {})
            return {
                "available": True,
                "keys": db_info.get("keys", 0),
                "url": self._url,
            }
        except redis.RedisError:
            return {"available": False}


# Singleton — import and use `cache` from anywhere in the gateway.
cache = DashboardCache()
