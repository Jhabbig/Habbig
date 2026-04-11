"""
Sliding window rate limiter with in-memory storage and optional Redis backend.

For single-worker deployments (uvicorn single process): in-memory is fine.
For multi-worker: each worker has independent counters — effective limit is
limit * num_workers. Use Redis (REDIS_URL env var) for cross-worker limiting.

Thread-safe via threading.Lock for the in-memory fallback.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from functools import wraps
from threading import Lock
from typing import Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() not in ("0", "false", "no", "off")


class SlidingWindowRateLimiter:
    """
    Sliding window rate limiter.

    Stores timestamps of requests per key. On each check, prunes timestamps
    outside the window, then counts remaining. If count >= limit, denied.
    """

    def __init__(self):
        self._windows: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()
        self._redis = None
        self._init_redis()
        self._last_cleanup = 0.0

    def _init_redis(self):
        """Try to connect to Redis. Silently fall back to in-memory."""
        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            try:
                import redis
                self._redis = redis.from_url(redis_url)
                self._redis.ping()
            except Exception:
                self._redis = None

    def check(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int, int]:
        """
        Check if a request is allowed.

        Returns: (allowed, remaining, retry_after_seconds)
        """
        if not RATE_LIMIT_ENABLED:
            return True, limit, 0

        now = time.time()

        if self._redis:
            return self._check_redis(key, limit, window_seconds, now)

        return self._check_memory(key, limit, window_seconds, now)

    def _check_memory(self, key: str, limit: int, window_seconds: int, now: float) -> tuple[bool, int, int]:
        # Periodic cleanup of stale keys (every 60s)
        if now - self._last_cleanup > 60:
            self._cleanup(now)

        with self._lock:
            window = self._windows[key]
            window_start = now - window_seconds

            # Remove expired timestamps
            while window and window[0] < window_start:
                window.popleft()

            count = len(window)
            if count >= limit:
                # Calculate when the oldest request in the window expires
                retry_after = int(window[0] - window_start) + 1 if window else window_seconds
                return False, 0, max(1, retry_after)

            window.append(now)
            return True, limit - count - 1, 0

    def _check_redis(self, key: str, limit: int, window_seconds: int, now: float) -> tuple[bool, int, int]:
        try:
            pipe = self._redis.pipeline()
            redis_key = f"rl:{key}"
            pipe.zadd(redis_key, {str(now): now})
            pipe.zremrangebyscore(redis_key, 0, now - window_seconds)
            pipe.zcard(redis_key)
            pipe.expire(redis_key, window_seconds + 10)
            results = pipe.execute()
            count = results[2]

            if count > limit:
                return False, 0, window_seconds
            return True, limit - count, 0
        except Exception:
            # Redis error — fall back to allowing the request
            return True, limit, 0

    def _cleanup(self, now: float):
        """Remove stale keys from the in-memory store."""
        self._last_cleanup = now
        cutoff = now - 7200  # Remove keys with no activity in 2 hours
        with self._lock:
            stale = [k for k, v in self._windows.items() if not v or v[-1] < cutoff]
            for k in stale:
                del self._windows[k]


# Global limiter instance
limiter = SlidingWindowRateLimiter()


def get_client_ip(request: Request) -> str:
    """
    Extract real client IP. Prefers Cloudflare's CF-Connecting-IP,
    then X-Forwarded-For first hop, then direct connection.
    """
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(
    limit: int,
    window_seconds: int,
    key_func: Optional[Callable[[Request], str]] = None,
    error_message: str = "Too many requests. Please try again later.",
):
    """
    Decorator for rate limiting FastAPI route handlers.

    key_func: callable(Request) -> str. Default: IP address.

    Usage:
        @rate_limit(limit=5, window_seconds=60)
        async def my_route(request: Request): ...

        @rate_limit(limit=10, window_seconds=3600,
                    key_func=lambda r: str(getattr(r.state, 'user_id', 'anon')))
        async def my_route(request: Request): ...
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Find request in args or kwargs
            request = kwargs.get("request")
            if request is None:
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break
            if request is None:
                return await func(*args, **kwargs)

            if key_func:
                # Custom key_func returns the full key — caller controls namespacing.
                # This lets multiple decorated handlers share the same bucket
                # (e.g. all auth routes hit one "auth:<ip>" key).
                key = key_func(request)
            else:
                key = f"{func.__module__}.{func.__name__}:{get_client_ip(request)}"

            allowed, remaining, retry_after = limiter.check(key, limit, window_seconds)

            if not allowed:
                from security.logger import log_rate_limit_hit
                log_rate_limit_hit(
                    key=key,
                    endpoint=request.url.path,
                    ip=get_client_ip(request),
                )
                return JSONResponse(
                    {"error": error_message},
                    status_code=429,
                    headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Limit": str(limit),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int(time.time()) + retry_after),
                    },
                )

            response = await func(*args, **kwargs)

            # Add rate limit headers to successful responses
            if hasattr(response, "headers"):
                response.headers["X-RateLimit-Limit"] = str(limit)
                response.headers["X-RateLimit-Remaining"] = str(remaining)

            return response
        return wrapper
    return decorator


def is_rate_limited(key: str, limit: int, window_seconds: int = 300) -> bool:
    """
    Simple check function for inline rate limiting (backwards compatible).
    Returns True if the key has exceeded the limit.
    """
    allowed, _, _ = limiter.check(key, limit, window_seconds)
    return not allowed


# Shared auth bucket: every auth route counts against the same per-IP key.
# 5 attempts per 15 minutes regardless of which auth endpoint is hit.
AUTH_RATE_LIMIT_COUNT = 5
AUTH_RATE_LIMIT_WINDOW = 900


def auth_rate_limit(
    error_message: str = "Too many authentication attempts. Please try again in 15 minutes.",
):
    """
    Decorator for authentication routes (login/signup/gate/invite/forgot-password/
    reset-password). All decorated routes share a single per-IP bucket so an
    attacker cannot multiply attempts by rotating between auth endpoints.
    """
    return rate_limit(
        limit=AUTH_RATE_LIMIT_COUNT,
        window_seconds=AUTH_RATE_LIMIT_WINDOW,
        key_func=lambda r: f"auth:{get_client_ip(r)}",
        error_message=error_message,
    )
