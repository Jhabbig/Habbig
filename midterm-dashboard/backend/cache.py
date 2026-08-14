"""
Redis client for the midterm-dashboard backend.

Provides three things:

* **rate-limit counter** — replaces the in-process ``defaultdict`` so quotas
  work correctly across uvicorn workers and process restarts.
* **pub/sub publisher** — fires ``data_updated`` events to the gateway's SSE
  bus and to in-process SSE consumers.
* **in-process subscribe** — async generator yielding messages on a channel,
  used by the local ``/data/stream`` SSE endpoint.

If Redis is unavailable (env var unset, or connection refused) every method
degrades to a sensible no-op or in-memory fallback. The dashboard keeps
running; the only visible effects are: rate limits become per-process again,
and SSE clients receive an "offline" notice instead of live events.

Mirrors the gateway's ``cache.py`` so the build has a single mental model
for Redis usage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from typing import AsyncGenerator, Optional

log = logging.getLogger("midterm.cache")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
DASHBOARD = "midterm"


class _InMemoryFallback:
    """Used when Redis is unreachable. Per-process state only."""

    def __init__(self):
        self._counters: dict[str, list[float]] = defaultdict(list)

    def rate_limit_check(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        cutoff = now - window_seconds
        self._counters[key] = [t for t in self._counters[key] if t > cutoff]
        if not self._counters[key]:
            del self._counters[key]
        elif len(self._counters[key]) >= limit:
            return False
        self._counters.setdefault(key, []).append(now)
        return True


class Cache:
    """Thin Redis wrapper with graceful fallback."""

    def __init__(self, url: str = REDIS_URL):
        self._url = url
        self._r = None  # type: ignore[assignment]
        self._available = False
        self._fallback = _InMemoryFallback()

    def connect(self) -> bool:
        try:
            import redis  # local import so the dashboard runs without the lib
        except ImportError:
            log.warning("redis library not installed — using in-memory fallback")
            return False
        try:
            self._r = redis.Redis.from_url(
                self._url,
                decode_responses=False,
                socket_connect_timeout=2,
                socket_timeout=1,
                retry_on_timeout=True,
            )
            self._r.ping()
            self._available = True
            import re as _re
            safe_url = _re.sub(r"://[^@]+@", "://****@", self._url)
            log.info("Redis connected at %s", safe_url)
            return True
        except Exception as e:
            log.warning("Redis unavailable (%s) — falling back to in-process state", e)
            self._available = False
            return False

    @property
    def available(self) -> bool:
        return self._available and self._r is not None

    # ── Rate limiting (sliding window) ───────────────────────────────────

    def rate_limit_check(self, identity: str, limit: int, window_seconds: int = 60) -> bool:
        """Return True if ``identity`` is within quota for the current window.

        Implementation: a Redis sorted set per identity, scored by timestamp.
        Stale entries are pruned on each call. Falls back to in-process when
        Redis is offline.
        """
        if limit <= 0:
            return True
        if not self.available:
            return self._fallback.rate_limit_check(identity, limit, window_seconds)
        try:
            now = time.time()
            key = f"midterm:rl:{identity}"
            pipe = self._r.pipeline()
            pipe.zremrangebyscore(key, 0, now - window_seconds)
            pipe.zcard(key)
            pipe.zadd(key, {str(now): now})
            pipe.expire(key, window_seconds + 5)
            _, count, _, _ = pipe.execute()
            return int(count) < limit
        except Exception as e:
            log.debug("Redis rate-limit error: %s — using fallback", e)
            return self._fallback.rate_limit_check(identity, limit, window_seconds)

    # ── Pub/sub ──────────────────────────────────────────────────────────

    def publish(self, event: str, data: Optional[dict] = None) -> None:
        """Fire a real-time event for the midterm dashboard."""
        if not self.available:
            return
        try:
            payload = json.dumps({
                "event": event,
                "dashboard": DASHBOARD,
                "ts": time.time(),
                "data": data or {},
            })
            self._r.publish(f"dashboard:{DASHBOARD}", payload)
        except Exception as e:
            log.debug("Redis publish error: %s", e)

    async def subscribe_async(
        self,
        heartbeat_interval: float = 15.0,
    ) -> AsyncGenerator[dict, None]:
        """Yield decoded pub/sub messages for the midterm channel.

        Yields ``{"event": ..., "data": ...}`` dicts. Sends periodic
        ``{"event": "heartbeat", ...}`` so callers can keep SSE connections
        alive through proxies. If Redis is offline, yields a single
        ``offline`` event then stops.
        """
        if not self.available:
            yield {"event": "offline", "msg": "Redis offline; live updates disabled"}
            return

        try:
            import redis  # noqa: F401
        except ImportError:
            yield {"event": "offline", "msg": "redis library missing"}
            return

        pubsub = self._r.pubsub()
        try:
            pubsub.subscribe(f"dashboard:{DASHBOARD}")
            yield {"event": "connected", "ts": time.time()}
            last_hb = time.time()
            while True:
                msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
                if msg and msg.get("type") == "message":
                    try:
                        payload = json.loads(msg["data"])
                        yield payload
                    except (json.JSONDecodeError, TypeError):
                        pass
                now = time.time()
                if now - last_hb >= heartbeat_interval:
                    yield {"event": "heartbeat", "ts": now}
                    last_hb = now
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Redis subscribe error: %s", e)
        finally:
            try:
                pubsub.unsubscribe(f"dashboard:{DASHBOARD}")
                pubsub.close()
            except Exception:
                pass


# Singleton — import and use ``cache`` from anywhere in the backend.
cache = Cache()
