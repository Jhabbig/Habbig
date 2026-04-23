"""Retry helper for external-service calls.

A tiny pure-stdlib alternative to `tenacity`. We can't add new wheels
to requirements.txt in fix-only passes, but we need the same API
shape so a later migration to tenacity is just an import swap.

Usage:

    from common.retry import retry, RetryableError

    @retry(
        stop_after_attempt=3,
        wait_exponential_min=1.0,
        wait_exponential_max=10.0,
        retry_on=(httpx.TimeoutException, httpx.ConnectError),
    )
    async def fetch_market(slug):
        ...

Rules:
  - 4xx client errors are **never** retried. Callers must raise a
    non-retryable exception type (e.g. pure ValueError) to signal
    "caller bug — don't waste cycles."
  - 429 rate-limit responses wait for the Retry-After header before
    the next attempt if a RetryAfter exception is raised (see
    `raise_for_retry_after`).
  - Every retried attempt logs at WARNING with attempt count + delay
    so ops can trace flappy upstreams.

The helper is intentionally minimal: no tenacity-style `RetryError`
wrapper (we re-raise the final exception verbatim) and no jitter
beyond the exponential spread.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from typing import Any, Callable, Iterable, Optional


log = logging.getLogger("gateway.retry")


class RetryAfter(Exception):
    """Raise from inside a wrapped fn to force a specific wait.

    Typically used when the upstream returns a 429 carrying a
    Retry-After header; the wrapped fn parses the header and raises
    `RetryAfter(seconds=...)`.
    """

    def __init__(self, *, seconds: float, reason: str = "rate limited") -> None:
        super().__init__(f"{reason}: retry after {seconds}s")
        self.seconds = max(0.0, float(seconds))
        self.reason = reason


def retry(
    *,
    stop_after_attempt: int = 3,
    wait_exponential_min: float = 1.0,
    wait_exponential_max: float = 10.0,
    retry_on: Optional[Iterable[type[BaseException]]] = None,
    name: Optional[str] = None,
) -> Callable:
    """Decorator factory. Exponential backoff between min and max."""
    retry_types: tuple[type[BaseException], ...] = (
        tuple(retry_on) if retry_on else (Exception,)
    )

    def _decorator(fn: Callable) -> Callable:
        label = name or fn.__name__

        def _delay_for(attempt: int, override: Optional[float]) -> float:
            if override is not None:
                return override
            # attempt is 1-indexed.
            d = wait_exponential_min * (2 ** (attempt - 1))
            return min(d, wait_exponential_max)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def _awrap(*args, **kwargs):
                last_exc: Optional[BaseException] = None
                for attempt in range(1, stop_after_attempt + 1):
                    try:
                        return await fn(*args, **kwargs)
                    except RetryAfter as ra:
                        last_exc = ra
                        if attempt >= stop_after_attempt:
                            break
                        delay = _delay_for(attempt, ra.seconds)
                        log.warning(
                            "retry[%s] attempt %d/%d: %s → sleep %.1fs",
                            label, attempt, stop_after_attempt, ra.reason, delay,
                        )
                        await asyncio.sleep(delay)
                    except retry_types as exc:
                        last_exc = exc
                        if attempt >= stop_after_attempt:
                            break
                        delay = _delay_for(attempt, None)
                        log.warning(
                            "retry[%s] attempt %d/%d: %s → sleep %.1fs",
                            label, attempt, stop_after_attempt,
                            exc.__class__.__name__, delay,
                        )
                        await asyncio.sleep(delay)
                if last_exc is not None:
                    raise last_exc
                # Shouldn't happen — loop always either returns or raises.
                raise RuntimeError("retry exhausted without capturing an exception")
            return _awrap

        @functools.wraps(fn)
        def _swrap(*args, **kwargs):
            last_exc: Optional[BaseException] = None
            for attempt in range(1, stop_after_attempt + 1):
                try:
                    return fn(*args, **kwargs)
                except RetryAfter as ra:
                    last_exc = ra
                    if attempt >= stop_after_attempt:
                        break
                    delay = _delay_for(attempt, ra.seconds)
                    log.warning(
                        "retry[%s] attempt %d/%d: %s → sleep %.1fs",
                        label, attempt, stop_after_attempt, ra.reason, delay,
                    )
                    time.sleep(delay)
                except retry_types as exc:
                    last_exc = exc
                    if attempt >= stop_after_attempt:
                        break
                    delay = _delay_for(attempt, None)
                    log.warning(
                        "retry[%s] attempt %d/%d: %s → sleep %.1fs",
                        label, attempt, stop_after_attempt,
                        exc.__class__.__name__, delay,
                    )
                    time.sleep(delay)
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("retry exhausted without capturing an exception")

        return _swrap

    return _decorator


def raise_for_retry_after(response: Any) -> None:
    """Convenience: if `response` carries a 429 + Retry-After, raise
    RetryAfter(seconds=...) so the @retry decorator backs off cleanly.

    Works with httpx.Response, starlette.Response, or any object
    exposing `.status_code` + `.headers.get()`.
    """
    status = getattr(response, "status_code", None)
    if status != 429:
        return
    headers = getattr(response, "headers", None)
    if headers is None:
        raise RetryAfter(seconds=1.0, reason="rate limited (no retry-after)")
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if not raw:
        raise RetryAfter(seconds=1.0, reason="rate limited")
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        # Retry-After can be an HTTP date; we don't parse those — fall
        # back to a conservative 5s.
        raise RetryAfter(seconds=5.0, reason="rate limited (http-date)")
    raise RetryAfter(seconds=secs, reason="rate limited")
