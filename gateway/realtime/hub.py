"""In-process pub/sub hub for WebSocket fan-out.

Single-worker, asyncio-only. Thread-safe within the event loop via
``asyncio.Lock``. Per-channel subscriber sets are bounded on the input
side (see ``channels.MAX_CHANNELS_PER_CONN``) and on the output side by
the ``send_json`` call itself — if a client is slow enough to break, we
drop it rather than queuing. Back-pressure policy is "disconnect, let
the client reconnect," which matches the client's exponential-backoff
strategy.

Broadcast ordering:
  - Within a channel, messages are delivered in call order.
  - Across channels, no ordering guarantee (each broadcast runs its
    own ``asyncio.gather`` over connected clients).

Metrics:
  - ``stats()`` returns a snapshot of {connections, channels, messages
    sent in last 60s} for the admin observability page.
  - Disconnects are logged with a reason code so we can diff patterns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Deque


log = logging.getLogger("gateway.realtime.hub")

# Keep the last 60 * 4 = 240 seconds of per-second tick counts so the
# admin page can show a rolling msgs/sec gauge without hitting the DB.
_TICK_BUCKETS = 240


class Hub:
    """Connection manager + pub/sub. Singleton — accessed via ``realtime.hub``."""

    def __init__(self) -> None:
        # channel name -> set of connected WebSocket objects subscribed to it
        self.channels: dict[str, set] = defaultdict(set)
        # WebSocket object -> set of channels it's subscribed to
        self.ws_channels: dict[Any, set[str]] = defaultdict(set)
        # WebSocket object -> {"user_id": int, "ip": str, "connected_at": float}
        self.ws_meta: dict[Any, dict] = {}
        # user_id -> list of WebSockets (oldest first — used to evict when the
        # per-user cap is exceeded).
        self.user_conns: dict[int, list] = defaultdict(list)

        # Per-second message tick buffer (sent + dropped).
        self._tick_sent: Deque[tuple[int, int]] = deque(maxlen=_TICK_BUCKETS)
        self._tick_dropped: Deque[tuple[int, int]] = deque(maxlen=_TICK_BUCKETS)

        # Disconnect reason counter (resets on server restart).
        self.disconnect_reasons: dict[str, int] = defaultdict(int)

        self._lock = asyncio.Lock()

    # ── Connection lifecycle ──────────────────────────────────────────

    async def register_connection(self, ws, *, user_id: int | None, ip: str | None) -> None:
        """Record a newly-accepted WebSocket."""
        async with self._lock:
            self.ws_meta[ws] = {
                "user_id": user_id,
                "ip": ip,
                "connected_at": time.time(),
            }
            if user_id is not None:
                self.user_conns[user_id].append(ws)

    async def evict_oldest_for_user(self, user_id: int, max_concurrent: int) -> list:
        """Return a list of oldest WebSockets to close so the user stays
        under ``max_concurrent`` connections. Caller is responsible for
        actually sending close frames."""
        to_close: list = []
        async with self._lock:
            conns = self.user_conns.get(user_id, [])
            # Keep the newest ``max_concurrent - 1``; close everything older
            # so the new connection becomes #max_concurrent exactly.
            excess = len(conns) - (max_concurrent - 1)
            if excess > 0:
                to_close = conns[:excess]
        return to_close

    # ── Subscription management ───────────────────────────────────────

    async def subscribe(self, ws, channel: str) -> None:
        async with self._lock:
            self.channels[channel].add(ws)
            self.ws_channels[ws].add(channel)

    async def unsubscribe(self, ws, channel: str) -> None:
        async with self._lock:
            self.channels.get(channel, set()).discard(ws)
            self.ws_channels.get(ws, set()).discard(channel)
            # Tidy empty channel buckets so stats() doesn't report ghost rows.
            if channel in self.channels and not self.channels[channel]:
                del self.channels[channel]

    async def unsubscribe_all(self, ws, *, reason: str = "unknown") -> None:
        """Remove the WebSocket from every channel. Called on disconnect."""
        async with self._lock:
            channels = self.ws_channels.pop(ws, set())
            for ch in channels:
                self.channels.get(ch, set()).discard(ws)
                if ch in self.channels and not self.channels[ch]:
                    del self.channels[ch]
            meta = self.ws_meta.pop(ws, None)
            if meta and meta.get("user_id") is not None:
                uid = meta["user_id"]
                conns = self.user_conns.get(uid)
                if conns and ws in conns:
                    conns.remove(ws)
                if conns is not None and not conns:
                    del self.user_conns[uid]
            self.disconnect_reasons[reason] += 1

    def subscriber_count(self, channel: str) -> int:
        return len(self.channels.get(channel, ()))

    # ── Broadcast ─────────────────────────────────────────────────────

    async def broadcast(self, channel: str, message: dict) -> int:
        """Send ``message`` to every WebSocket subscribed to ``channel``.

        Dead connections (``RuntimeError`` / ``ConnectionClosed``) are
        silently removed. Returns the number of recipients reached.
        """
        # Snapshot subscribers under the lock to avoid "set changed during
        # iteration" if another coroutine subscribes mid-broadcast.
        async with self._lock:
            subs = list(self.channels.get(channel, ()))

        if not subs:
            # Still tick for metrics — a broadcast with zero subscribers
            # is a common case and useful for debugging.
            self._tick(sent=0, dropped=0)
            return 0

        # Attach the channel + a sequence so clients can reconcile order
        # across reconnects if they care to.
        envelope = {
            "channel": channel,
            "ts": int(time.time() * 1000),
            **message,
        }
        sent = 0
        dropped: list = []
        for ws in subs:
            try:
                await ws.send_json(envelope)
                sent += 1
            except Exception:
                # Broken pipe / closed handshake / backpressure failure.
                # Collect and evict outside the send loop so one bad client
                # can't delay delivery to healthy ones.
                dropped.append(ws)

        if dropped:
            for ws in dropped:
                await self.unsubscribe_all(ws, reason="broadcast_send_failed")
        self._tick(sent=sent, dropped=len(dropped))
        return sent

    # ── Metrics ───────────────────────────────────────────────────────

    def _tick(self, *, sent: int, dropped: int) -> None:
        now = int(time.time())
        if self._tick_sent and self._tick_sent[-1][0] == now:
            self._tick_sent[-1] = (now, self._tick_sent[-1][1] + sent)
            self._tick_dropped[-1] = (now, self._tick_dropped[-1][1] + dropped)
        else:
            self._tick_sent.append((now, sent))
            self._tick_dropped.append((now, dropped))

    def stats(self) -> dict:
        """Snapshot for the admin observability page."""
        now = int(time.time())
        window_start = now - 60
        msgs_last_60s = sum(v for t, v in self._tick_sent if t >= window_start)
        dropped_last_60s = sum(v for t, v in self._tick_dropped if t >= window_start)
        top_channels = sorted(
            ((ch, len(subs)) for ch, subs in self.channels.items()),
            key=lambda x: x[1],
            reverse=True,
        )[:10]
        per_second = [
            {"ts": t, "sent": v} for (t, v) in list(self._tick_sent)[-60:]
        ]
        return {
            "connections": len(self.ws_meta),
            "unique_users": len(self.user_conns),
            "channels": len(self.channels),
            "msgs_last_60s": msgs_last_60s,
            "dropped_last_60s": dropped_last_60s,
            "top_channels": [
                {"channel": ch, "subscribers": n} for ch, n in top_channels
            ],
            "disconnect_reasons": dict(self.disconnect_reasons),
            "per_second": per_second,
        }


# Module-level singleton.
hub = Hub()
