"""In-app notification runtime — preference-gated writes + SSE fan-out.

The bell feature has three layers:

  1. ``db.create_notification`` — raw insert, no gating (see db.py).
  2. ``notifications.create_notification`` (this module) — checks the
     user's preferences and, on success, broadcasts the new row to every
     SSE subscriber for that user. Callers (existing job triggers, admin
     actions, webhook handlers) use THIS function, never the db one.
  3. ``notification_routes.py`` — HTTP / SSE endpoints consumed by the
     bell dropdown and /notifications page.

SSE fan-out is in-process only (one queue per connection, registered in
``_subscribers``). Gateway runs single-process with SQLite so there's no
cross-worker routing to worry about. If you later move to gunicorn-with-
workers or horizontal scale-out, swap ``_broadcast`` for Redis pub/sub
— the rest of the module stays the same.

Preference gating has three doors:
  * ``inapp_enabled`` off → nothing persists, broadcast skipped.
  * ``types[type]`` off    → same.
  * Neither of those guards is re-checked in the SSE stream itself —
    if a row made it into the DB we deliver it.

Failures are swallowed (logged, not raised). A broken email job shouldn't
take the notification system down, and the user can still see the row
via the REST API fallback even if the live push misses them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Optional

import db


log = logging.getLogger("notifications")


# ── SSE subscriber registry ─────────────────────────────────────────────────
# Each HTTP SSE connection creates a bounded queue and appends it here. When
# a notification is created we drop the payload into every queue for the
# target user. The asyncio.Lock serialises mutations of the dict + list so a
# concurrent subscribe/unsubscribe can't leave a dangling queue.
#
# Bounded to 100 items so a stalled browser tab can't blow unbounded RAM.
# When full we drop and log; the client will catch up via the REST list API
# on reconnect.

_subscribers: dict[int, list[asyncio.Queue]] = {}
_lock: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    """Lazy lock — we can't create one at import time because there's not
    always a running event loop. Safe to call from any coroutine."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def subscribe(user_id: int, *, maxsize: int = 100) -> asyncio.Queue:
    """Register a new SSE subscriber queue for ``user_id``."""
    q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    async with _get_lock():
        _subscribers.setdefault(user_id, []).append(q)
    return q


async def unsubscribe(user_id: int, q: asyncio.Queue) -> None:
    """Drop the queue from the registry. Safe on double-unsubscribe."""
    async with _get_lock():
        queues = _subscribers.get(user_id) or []
        try:
            queues.remove(q)
        except ValueError:
            pass
        if not queues:
            _subscribers.pop(user_id, None)


async def _broadcast(user_id: int, payload: dict) -> None:
    """Fan-out a payload to every live subscriber for the user."""
    async with _get_lock():
        queues = list(_subscribers.get(user_id) or [])
    for q in queues:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("notifications: SSE queue full user_id=%s — dropping", user_id)


def _has_subscribers(user_id: int) -> bool:
    """Test helper: returns True if any SSE queue is currently registered
    for ``user_id``. Used by tests to assert broadcast fan-out without
    opening a real HTTP connection."""
    return bool(_subscribers.get(user_id))


# ── Public API ──────────────────────────────────────────────────────────────

async def create_notification(
    user_id: int,
    type: str,
    title: str,
    body: str = "",
    *,
    link_url: Optional[str] = None,
    icon: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Optional[int]:
    """Preference-aware insert + broadcast.

    Returns the new notification id, or None if the user has opted out of
    this type (or disabled in-app notifications wholesale). Never raises —
    persistence failures log and return None so the caller (usually a
    job trigger) keeps going with its primary work (email send, etc.).
    """
    if type not in db.NOTIFICATION_TYPES:
        log.warning("create_notification: unknown type %s, coercing to 'system'", type)
        type = "system"

    # Preference gate — empty results mean defaults = all-on.
    try:
        if not db.notification_type_enabled(user_id, type):
            return None
    except Exception:
        # Reading prefs shouldn't fail, but if it does we prefer to deliver
        # than to silently drop.
        log.exception("notifications: prefs lookup failed user=%s, delivering anyway", user_id)

    try:
        notif_id = db.create_notification(
            user_id=user_id,
            type=type,
            title=title,
            body=body,
            link_url=link_url,
            icon=icon,
            metadata=metadata,
        )
    except Exception:
        log.exception("notifications: insert failed user=%s type=%s", user_id, type)
        return None

    # Build the broadcast payload from scratch rather than re-reading the row
    # we just wrote — saves a round-trip and avoids a torn read on a slow DB.
    payload = {
        "id":         notif_id,
        "user_id":    user_id,
        "type":       type,
        "title":      title,
        "body":       body or "",
        "link_url":   link_url,
        "icon":       icon,
        "metadata":   metadata or {},
        "created_at": int(time.time()),
        "read_at":    None,
        "archived":   False,
    }
    try:
        await _broadcast(user_id, payload)
    except Exception:
        log.exception("notifications: broadcast failed id=%s", notif_id)
    return notif_id


# ── SSE stream generator ────────────────────────────────────────────────────

def sse_format(event: str, data: Any) -> str:
    """Serialise one SSE event. Newline-terminated per the spec."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def sse_stream(user_id: int, *, heartbeat_seconds: int = 30) -> AsyncIterator[str]:
    """Yield SSE-formatted strings for one subscriber.

    Emits:
      * ``event: ping`` once on connect so the client knows the stream is live
      * ``event: notification`` for each new notification
      * ``event: heartbeat`` every ``heartbeat_seconds`` of idle time so
        upstream proxies (Cloudflare, etc.) don't eat an idle connection.

    Caller (route handler) wraps this generator in a ``StreamingResponse``
    with ``media_type='text/event-stream'``. Clean-up of the subscriber
    queue runs in ``finally`` so a client disconnect always frees the slot.
    """
    q = await subscribe(user_id)
    try:
        # Opening ping so the client can show "Live" state immediately.
        yield sse_format("ping", {"ts": int(time.time())})
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=heartbeat_seconds)
                yield sse_format("notification", payload)
            except asyncio.TimeoutError:
                yield sse_format("heartbeat", {"ts": int(time.time())})
    except (asyncio.CancelledError, GeneratorExit):
        raise
    finally:
        await unsubscribe(user_id, q)
