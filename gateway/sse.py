"""
Server-Sent Events (SSE) stream for real-time dashboard updates.

The gateway subscribes to Redis pub/sub channels (one per dashboard).
Connected browsers receive events like:

    event: data_updated
    data: {"dashboard":"sports","ts":1712500000.0}

Frontend JS listens on EventSource("/api/stream?dashboards=sports,crypto")
and triggers a data refresh when it receives an event for its dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Optional

from cache import cache

log = logging.getLogger("gateway.sse")

# Track connected clients for the admin stats endpoint.
_active_connections = 0


def active_connection_count() -> int:
    return _active_connections


async def event_stream(
    dashboards: list[str],
    heartbeat_interval: int = 15,
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted strings. One generator per connected client.

    Args:
        dashboards: Which dashboard channels to subscribe to.
        heartbeat_interval: Seconds between keep-alive pings.
    """
    global _active_connections
    _active_connections += 1

    pubsub = cache.pubsub()
    if pubsub is None:
        # Redis unavailable — send an error event then close.
        yield _format_sse("error", {"msg": "Real-time updates unavailable (Redis offline)"})
        _active_connections -= 1
        return

    channels = [f"dashboard:{d}" for d in dashboards]
    try:
        pubsub.subscribe(*channels)
        log.debug("SSE client subscribed to %s", channels)

        # Initial connection event so the client knows the stream is live.
        yield _format_sse("connected", {
            "dashboards": dashboards,
            "ts": time.time(),
        })

        last_heartbeat = time.time()

        while True:
            # Non-blocking poll — check for a message, then yield control.
            msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)

            if msg and msg["type"] == "message":
                try:
                    payload = json.loads(msg["data"])
                    yield _format_sse(
                        payload.get("event", "data_updated"),
                        payload,
                    )
                except (json.JSONDecodeError, TypeError):
                    pass

            # Heartbeat keeps the connection alive through proxies / Cloudflare.
            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                yield _format_sse("heartbeat", {"ts": now})
                last_heartbeat = now

            # Yield control to the event loop so other requests aren't starved.
            await asyncio.sleep(0.2)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning("SSE stream error: %s", e)
    finally:
        try:
            pubsub.unsubscribe(*channels)
            pubsub.close()
        except Exception:
            pass
        _active_connections -= 1
        log.debug("SSE client disconnected (channels=%s)", channels)


def _format_sse(event: str, data: dict) -> str:
    """Format a single SSE frame."""
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"
