"""Simple in-process circuit breaker.

Used to wrap calls to upstream services that can cascade-fail (Claude,
Stripe, Polymarket, Kalshi, SEC EDGAR). When an upstream has returned
N consecutive failures within the recovery window the breaker opens
and subsequent calls fail fast — saving the worker pool and letting
the upstream recover.

State machine:

    closed ── N failures ───▶ open ── recovery_timeout ──▶ half-open
       ▲                                                      │
       └──────────────── success ─────────────────────────────┘

  - closed: normal operation, failures count up
  - open:   can_call() returns False; every call short-circuits
  - half-open: one probe call allowed; on success → closed, on
               failure → re-opens for another recovery_timeout

Thread-safe: tiny enough that a single RLock around mutation works.
Per-process: if you run multiple workers the breaker is per-worker —
which is the right default because you don't want one slow worker to
open the breaker for every other worker that's healthy.

Usage:

    from common.circuit_breaker import CircuitBreaker, CircuitOpen

    claude_breaker = CircuitBreaker(name="claude", failure_threshold=5, recovery_timeout=60)

    async def call_claude(...):
        if not claude_breaker.can_call():
            raise CircuitOpen("claude upstream is down")
        try:
            result = await upstream.call(...)
        except Exception:
            claude_breaker.record_failure()
            raise
        claude_breaker.record_success()
        return result

Or use the decorator:

    @claude_breaker.wrap()
    async def call_claude(...):
        ...
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from typing import Any, Callable, Iterable, Optional


log = logging.getLogger("gateway.circuit_breaker")


class CircuitOpen(RuntimeError):
    """Raised when a call is rejected because the breaker is open.

    Caller should either return a cached fallback or surface a 503 to
    the client. Never re-map to a generic 500 — 503 signals "transient,
    retry later" which is what we actually mean.
    """


class CircuitBreaker:
    def __init__(
        self,
        *,
        name: str = "breaker",
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        expected_exceptions: Optional[Iterable[type[BaseException]]] = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exceptions: tuple[type[BaseException], ...] = (
            tuple(expected_exceptions) if expected_exceptions else (Exception,)
        )
        self._lock = threading.RLock()
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._half_open_probe_in_flight = False
        # Counters for /debug + tests.
        self.open_count = 0
        self.rejected_count = 0
        self.success_count = 0
        self.failure_count = 0

    # ── State queries ────────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if time.time() - self._opened_at > self.recovery_timeout:
                return "half_open"
            return "open"

    def can_call(self) -> bool:
        """Return True if the caller is allowed through.

        Transitions the breaker from open → half-open when the recovery
        timeout has elapsed. In half-open state only a single probe is
        permitted at a time; concurrent calls are rejected until the
        probe resolves.
        """
        with self._lock:
            if self._opened_at is None:
                return True
            now = time.time()
            if now - self._opened_at > self.recovery_timeout:
                # Window elapsed — allow exactly one probe through.
                if self._half_open_probe_in_flight:
                    self.rejected_count += 1
                    return False
                self._half_open_probe_in_flight = True
                return True
            self.rejected_count += 1
            return False

    def record_success(self) -> None:
        with self._lock:
            self.success_count += 1
            # Any success (including a half-open probe) fully closes
            # the breaker.
            if self._opened_at is not None:
                log.info("circuit_breaker[%s] closed after probe success", self.name)
            self._failures = 0
            self._opened_at = None
            self._half_open_probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            self._failures += 1
            # Half-open probe failed — re-open immediately.
            if self._half_open_probe_in_flight:
                self._half_open_probe_in_flight = False
                self._opened_at = time.time()
                self.open_count += 1
                log.warning("circuit_breaker[%s] re-opened after probe failure", self.name)
                return
            if self._failures >= self.failure_threshold and self._opened_at is None:
                self._opened_at = time.time()
                self.open_count += 1
                log.warning(
                    "circuit_breaker[%s] opened after %d consecutive failures",
                    self.name, self._failures,
                )

    def reset(self) -> None:
        """Force-reset (tests, manual ops tool)."""
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._half_open_probe_in_flight = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "state": self.state,
                "failures": self._failures,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout": self.recovery_timeout,
                "opened_at": self._opened_at,
                "open_count": self.open_count,
                "rejected_count": self.rejected_count,
                "success_count": self.success_count,
                "failure_count": self.failure_count,
            }

    # ── Decorator / context-manager helpers ──────────────────────────

    def wrap(self) -> Callable:
        """Decorator form. Sync and async functions both supported."""
        import asyncio

        def _decorator(fn):
            if asyncio.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def _awrap(*args, **kwargs):
                    if not self.can_call():
                        raise CircuitOpen(f"{self.name} breaker is open")
                    try:
                        out = await fn(*args, **kwargs)
                    except self.expected_exceptions:
                        self.record_failure()
                        raise
                    self.record_success()
                    return out
                return _awrap

            @functools.wraps(fn)
            def _swrap(*args, **kwargs):
                if not self.can_call():
                    raise CircuitOpen(f"{self.name} breaker is open")
                try:
                    out = fn(*args, **kwargs)
                except self.expected_exceptions:
                    self.record_failure()
                    raise
                self.record_success()
                return out
            return _swrap

        return _decorator


# ── Named breakers ───────────────────────────────────────────────────
# One per upstream. Imported by callers so they share state across
# handlers. Tune the thresholds per service — Claude is slow so 5
# consecutive failures is ~a minute of badness; Stripe is usually
# fast so 3 is enough.

claude_breaker = CircuitBreaker(name="claude", failure_threshold=5, recovery_timeout=60)
stripe_breaker = CircuitBreaker(name="stripe", failure_threshold=3, recovery_timeout=30)
polymarket_breaker = CircuitBreaker(name="polymarket", failure_threshold=5, recovery_timeout=60)
kalshi_breaker = CircuitBreaker(name="kalshi", failure_threshold=5, recovery_timeout=60)
sec_edgar_breaker = CircuitBreaker(name="sec_edgar", failure_threshold=3, recovery_timeout=120)


def all_breakers() -> list[CircuitBreaker]:
    """Return every named breaker — handy for /admin/debug status."""
    return [
        claude_breaker,
        stripe_breaker,
        polymarket_breaker,
        kalshi_breaker,
        sec_edgar_breaker,
    ]
