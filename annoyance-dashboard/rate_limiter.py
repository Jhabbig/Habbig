"""Sliding-window rate limiter for the annoyance dashboard.

Adapted from gateway/security/rate_limiter.py but slimmed down — we don't
need Redis here (single uvicorn process) and we don't need a decorator
since the routes are short and wiring is explicit.

Keys:
  * authenticated requests  → ``user:<id>``
  * unauthenticated        → ``ip:<client_ip>``

Cloudflare's CF-Connecting-IP / X-Forwarded-For are respected so a bad
actor behind the same edge can't share a bucket with a legitimate user.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Optional

from fastapi import HTTPException, Request


RATE_LIMIT_ENABLED = os.environ.get(
    "RATE_LIMIT_ENABLED", "true"
).lower() not in ("0", "false", "no", "off")


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._windows: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()
        self._last_cleanup = 0.0

    def check(
        self, key: str, limit: int, window_seconds: int
    ) -> tuple[bool, int, int]:
        """Return (allowed, remaining, retry_after_seconds)."""
        if not RATE_LIMIT_ENABLED:
            return True, limit, 0

        now = time.time()
        if now - self._last_cleanup > 60:
            self._cleanup(now)

        with self._lock:
            window = self._windows[key]
            window_start = now - window_seconds
            while window and window[0] < window_start:
                window.popleft()
            count = len(window)
            if count >= limit:
                retry_after = int(window[0] - window_start) + 1 if window else window_seconds
                return False, 0, max(1, retry_after)
            window.append(now)
            return True, limit - count - 1, 0

    def _cleanup(self, now: float) -> None:
        self._last_cleanup = now
        cutoff = now - 7200
        with self._lock:
            stale = [k for k, v in self._windows.items() if not v or v[-1] < cutoff]
            for k in stale:
                del self._windows[k]

    def reset(self) -> None:
        """Test helper — wipe all state."""
        with self._lock:
            self._windows.clear()
            self._last_cleanup = 0.0


_limiter = SlidingWindowRateLimiter()


def get_client_ip(request: Request) -> str:
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_key(request: Request, user: Optional[dict]) -> str:
    """Derive the rate-limit bucket for the current request."""
    if user and "id" in user:
        return f"user:{user['id']}"
    return f"ip:{get_client_ip(request)}"


def enforce(
    request: Request,
    user: Optional[dict],
    *,
    limit: int,
    window_seconds: int,
    scope: str,
) -> None:
    """Check the limit. Raises HTTPException(429) when over budget.

    ``scope`` namespaces the key so /api/fp-flag (10/min) can't deplete
    the same bucket as /api/index (60/min).
    """
    key = f"{scope}:{rate_key(request, user)}"
    allowed, remaining, retry_after = _limiter.check(key, limit, window_seconds)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limit_exceeded",
                "scope": scope,
                "retry_after": retry_after,
            },
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(time.time()) + retry_after),
            },
        )


def reset_for_tests() -> None:
    _limiter.reset()


# Scope presets so server.py stays readable.
DEFAULT_API_LIMIT = 60          # reqs per minute
DEFAULT_API_WINDOW = 60
FP_FLAG_LIMIT = 10
FP_FLAG_WINDOW = 60
