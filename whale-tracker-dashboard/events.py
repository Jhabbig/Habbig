"""In-process pub/sub for SSE live stream.

Subscribers each get an asyncio.Queue. The ingest loop calls
`broadcast()` after each pass with a summary; the SSE handler in
`server.py` drains queues and emits SSE frames.

Bounded queues drop old events on overflow rather than blocking the
broadcaster — a slow client should never wedge ingest.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("events")

_QUEUE_MAX = 64
_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def broadcast(event_type: str, payload: dict[str, Any]) -> int:
    msg = {"type": event_type, "payload": payload}
    delivered = 0
    full_queues: list[asyncio.Queue] = []
    for q in _subscribers:
        try:
            q.put_nowait(msg)
            delivered += 1
        except asyncio.QueueFull:
            full_queues.append(q)
    # Drop oldest from full queues and retry once so slow clients see
    # the latest event rather than getting stuck on a stale one.
    for q in full_queues:
        try:
            _ = q.get_nowait()
            q.put_nowait(msg)
            delivered += 1
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass
    return delivered


def subscriber_count() -> int:
    return len(_subscribers)
