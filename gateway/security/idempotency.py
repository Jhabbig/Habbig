"""Idempotency-key helper for subscription-critical writes.

The protection scope is narrow: a client retry (network blip, double-
click on "Subscribe", mobile app offline-and-resync) should never
produce a duplicate subscription, duplicate charge, or duplicate
cancellation. Idempotency is implemented as a short-lived ledger
keyed on (user_id, operation, client_key): if the same triple
appears within the TTL window, the second call returns the cached
first response.

This is *not* long-term idempotency. For Stripe webhooks the
``processed_stripe_events`` table covers the multi-day retry window;
this module covers the in-session "retry within 10s" window.

Storage is Redis when available (so horizontal workers see one
another) and in-process otherwise — same pattern as the rest of
gateway/security/.

Caller shape:

    from security.idempotency import with_idempotency

    @app.post("/api/billing/subscribe")
    async def subscribe(request: Request):
        user = _current_user(request)
        key = request.headers.get("Idempotency-Key")
        async def _do():
            ...     # actual work
            return {"subscription_id": ...}
        return await with_idempotency(
            user_id=user["id"],
            op="subscribe",
            client_key=key,
            ttl_seconds=10,
            body=_do,
        )
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from threading import Lock
from typing import Any, Awaitable, Callable, Optional


log = logging.getLogger("security.idempotency")


# ── In-process store (fallback when no Redis) ──────────────────────────────

_store: dict[str, tuple[float, str]] = {}
_store_lock = Lock()
_last_gc = 0.0


def _gc_if_needed() -> None:
    """Cheap periodic GC so the dict doesn't grow forever. Idempotency
    keys TTL in seconds, so we sweep once a minute."""
    global _last_gc
    now = time.time()
    if now - _last_gc < 60:
        return
    _last_gc = now
    with _store_lock:
        stale = [k for k, (expires, _) in _store.items() if expires < now]
        for k in stale:
            _store.pop(k, None)


def _memory_get(key: str) -> Optional[str]:
    _gc_if_needed()
    with _store_lock:
        entry = _store.get(key)
    if not entry:
        return None
    expires, value = entry
    if expires < time.time():
        with _store_lock:
            _store.pop(key, None)
        return None
    return value


def _memory_set(key: str, value: str, ttl_seconds: int) -> None:
    with _store_lock:
        _store[key] = (time.time() + ttl_seconds, value)


# ── Redis backend (same approach as security/rate_limiter.py) ──────────────

_redis = None
_redis_init = False


def _get_redis():
    """Lazy-init the Redis connection. Sticky-failure: if the first
    attempt fails we stay on the in-process store forever (retrying
    per-request would amplify the outage)."""
    global _redis, _redis_init
    if _redis_init:
        return _redis
    _redis_init = True
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        import redis  # type: ignore[import]
        client = redis.from_url(url, socket_timeout=0.5)
        client.ping()
        _redis = client
    except Exception as exc:  # pragma: no cover
        log.warning("idempotency: Redis init failed (%s); using in-memory", exc)
        _redis = None
    return _redis


# ── Key construction ───────────────────────────────────────────────────────

def _derive_key(
    *, user_id: int, op: str, client_key: Optional[str], fallback_fingerprint: Optional[str],
) -> str:
    """Build the ledger key.

    If the caller supplied an ``Idempotency-Key`` header we trust it
    (scoped by user + op so two users using the same token don't
    collide). If not, we fall back to a hash of
    ``fallback_fingerprint`` (usually the request body) so a
    too-fast-double-click still catches. No fingerprint → no
    idempotency.
    """
    if client_key:
        trimmed = client_key.strip()[:128]
        return f"idem:{user_id}:{op}:client:{trimmed}"
    if fallback_fingerprint:
        h = hashlib.sha256(fallback_fingerprint.encode("utf-8")).hexdigest()[:24]
        return f"idem:{user_id}:{op}:hash:{h}"
    return ""


# ── Public API ─────────────────────────────────────────────────────────────


async def with_idempotency(
    *,
    user_id: int,
    op: str,
    client_key: Optional[str],
    ttl_seconds: int,
    body: Callable[[], Awaitable[Any]],
    fallback_fingerprint: Optional[str] = None,
) -> Any:
    """Run `body()` at most once per (user_id, op, key) within `ttl_seconds`.

    First call: runs body, JSON-serialises the result, stores it.
    Second call (same key inside TTL): returns the stored result
    without running body.

    `body` is required to return something JSON-serialisable (or
    None). Side-effects happen inside `body`; the second caller sees
    the same return value but does NOT re-trigger the side-effects.

    If no key can be derived (neither `client_key` nor
    `fallback_fingerprint`), we skip the cache and just run body —
    the feature degrades open so missing headers don't lock users
    out of critical writes.
    """
    key = _derive_key(
        user_id=user_id, op=op,
        client_key=client_key,
        fallback_fingerprint=fallback_fingerprint,
    )
    if not key:
        return await body()

    cached = _load(key)
    if cached is not None:
        return cached

    result = await body()
    try:
        _store_result(key, result, ttl_seconds)
    except Exception as exc:  # pragma: no cover
        # Storage failures are logged but do NOT undo the side-
        # effects the body already performed.
        log.warning("idempotency: store failed for %s: %s", op, exc)
    return result


def _load(key: str) -> Any:
    redis = _get_redis()
    if redis is not None:
        try:
            raw = redis.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:  # pragma: no cover
            log.warning("idempotency: Redis get failed (%s); fallback", exc)
    raw_str = _memory_get(key)
    if raw_str is None:
        return None
    try:
        return json.loads(raw_str)
    except (TypeError, ValueError):
        return None


def _store_result(key: str, value: Any, ttl_seconds: int) -> None:
    try:
        encoded = json.dumps(value, default=str)
    except (TypeError, ValueError) as exc:
        # Result isn't JSON-serialisable — log but don't cache.
        # Caller will still get the correct return value.
        log.warning("idempotency: skip cache (unserialisable): %s", exc)
        return
    redis = _get_redis()
    if redis is not None:
        try:
            redis.set(key, encoded, ex=ttl_seconds)
            return
        except Exception as exc:  # pragma: no cover
            log.warning("idempotency: Redis set failed (%s); fallback", exc)
    _memory_set(key, encoded, ttl_seconds)


# ── Reset (test-only) ──────────────────────────────────────────────────────


def reset_for_tests() -> None:
    """Unit tests drop both backends to avoid cross-test leakage."""
    global _last_gc
    with _store_lock:
        _store.clear()
        _last_gc = 0.0
    redis = _get_redis()
    if redis is not None:
        try:
            # Wipe our namespace only — never a FLUSHDB.
            for k in redis.scan_iter(match="idem:*", count=500):
                redis.delete(k)
        except Exception:  # pragma: no cover
            pass
