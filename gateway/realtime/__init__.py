"""Real-time pub/sub infrastructure.

Single WebSocket endpoint (``/ws``) carries every live-update stream in
the app — market ticks, new predictions, user notifications, credibility
recomputes, admin security events. Replaces the polling + SSE approach
that preceded it.

Shape:
    client ──ws──► :7000/ws ──► realtime.routes.ws_endpoint
                                      │
                                      ▼
                         realtime.hub.hub (singleton Hub)
                                      │
                                      ├─► channel dispatch
                                      └─► back to subscribed clients

Call sites broadcast via ``hub.broadcast(channel, message)``; they never
touch WebSockets directly. The hub itself is in-process — no Redis
dependency for single-worker deploys. If/when we scale to multi-worker
we'll swap ``Hub`` for a Redis-backed pub/sub with the same interface.
"""

from __future__ import annotations

from .hub import hub
from .channels import is_channel_allowed, CHANNEL_PATTERNS

__all__ = ["hub", "is_channel_allowed", "CHANNEL_PATTERNS", "register"]


def register(app) -> None:
    """Wire the realtime WebSocket endpoint + admin stats endpoint into the app."""
    from . import routes as _routes
    _routes.register(app)
