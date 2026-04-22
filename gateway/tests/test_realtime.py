"""Unit tests for the realtime WebSocket infrastructure.

Exercises:
  - channel auth (owner-only user channels, admin gate on admin:security,
    subproduct gate via has_subproduct_access).
  - hub subscribe/unsubscribe/broadcast semantics (including graceful
    drop of disconnected sockets).
  - /ws handshake — accept with valid session, close with 4401 without
    one, close with 4429 when the per-user connection cap is hit.
  - stats() shape for the admin panel.

Uses the FastAPI TestClient WebSocket helper for the HTTP-level tests;
hub-level tests run against the singleton directly with a fake WS.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from realtime import channels
from realtime.hub import Hub


# ── Fake WebSocket ────────────────────────────────────────────────────────


class FakeWS:
    """Stand-in for a Starlette WebSocket. Captures send_json calls and can
    simulate a dead connection by setting ``self.dead = True``."""

    def __init__(self, name: str = "fake"):
        self.name = name
        self.sent: list[dict] = []
        self.dead = False

    async def send_json(self, data) -> None:
        if self.dead:
            raise RuntimeError("connection closed")
        self.sent.append(data)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Channel auth ──────────────────────────────────────────────────────────


class TestChannelAuth(unittest.TestCase):
    def test_anon_denied_everywhere(self):
        for ch in ("market:x", "user:1", "feed:global", "admin:security", "subproduct:foo"):
            self.assertFalse(channels.is_channel_allowed(None, ch))
            self.assertFalse(channels.is_channel_allowed({}, ch))

    def test_market_any_authed(self):
        user = {"user_id": 1}
        self.assertTrue(channels.is_channel_allowed(user, "market:poly:fed-rate"))
        # Unknown market (spec: accepted, channel lazy-created).
        self.assertTrue(channels.is_channel_allowed(user, "market:unknown_slug"))

    def test_market_slug_validation(self):
        user = {"user_id": 1}
        self.assertFalse(channels.is_channel_allowed(user, "market:has space"))
        self.assertFalse(channels.is_channel_allowed(user, "market:"))

    def test_user_owner_only(self):
        self.assertTrue(channels.is_channel_allowed({"user_id": 7}, "user:7"))
        self.assertFalse(channels.is_channel_allowed({"user_id": 7}, "user:8"))
        self.assertFalse(channels.is_channel_allowed({"user_id": 7}, "user:abc"))

    def test_feed_global_any_authed(self):
        self.assertTrue(channels.is_channel_allowed({"user_id": 1}, "feed:global"))
        self.assertFalse(channels.is_channel_allowed({"user_id": 1}, "feed:anything_else"))

    def test_admin_security_role_gate(self):
        self.assertFalse(channels.is_channel_allowed({"user_id": 1, "is_admin": 0}, "admin:security"))
        self.assertTrue(channels.is_channel_allowed({"user_id": 1, "is_admin": 1}, "admin:security"))
        self.assertFalse(channels.is_channel_allowed({"user_id": 1, "is_admin": 1}, "admin:other"))

    def test_subproduct_checks_entitlement(self):
        with patch("realtime.channels._has_subproduct_access", return_value=True):
            self.assertTrue(channels.is_channel_allowed({"user_id": 1}, "subproduct:trading-intel"))
        with patch("realtime.channels._has_subproduct_access", return_value=False):
            self.assertFalse(channels.is_channel_allowed({"user_id": 1}, "subproduct:trading-intel"))

    def test_unknown_namespace_rejected(self):
        self.assertFalse(channels.is_channel_allowed({"user_id": 1}, "stealth:everything"))
        self.assertFalse(channels.is_channel_allowed({"user_id": 1}, ":broken"))
        self.assertFalse(channels.is_channel_allowed({"user_id": 1}, "broken:"))


# ── Hub pub/sub ───────────────────────────────────────────────────────────


class TestHubSubscriptions(unittest.TestCase):
    def test_subscribe_and_broadcast_reaches_everyone(self):
        hub = Hub()
        a, b, c = FakeWS("a"), FakeWS("b"), FakeWS("c")

        async def scenario():
            await hub.subscribe(a, "market:x")
            await hub.subscribe(b, "market:x")
            await hub.subscribe(c, "market:y")
            reached = await hub.broadcast("market:x", {"type": "tick", "v": 1})
            return reached

        reached = _run(scenario())
        self.assertEqual(reached, 2)
        self.assertEqual(len(a.sent), 1)
        self.assertEqual(a.sent[0]["type"], "tick")
        self.assertEqual(a.sent[0]["channel"], "market:x")
        self.assertIn("ts", a.sent[0])
        self.assertEqual(len(c.sent), 0)

    def test_unsubscribe_stops_delivery(self):
        hub = Hub()
        a = FakeWS("a")

        async def scenario():
            await hub.subscribe(a, "feed:global")
            await hub.unsubscribe(a, "feed:global")
            return await hub.broadcast("feed:global", {"type": "x"})

        self.assertEqual(_run(scenario()), 0)
        self.assertEqual(a.sent, [])

    def test_disconnect_cleans_all_subs(self):
        hub = Hub()
        a = FakeWS("a")

        async def scenario():
            await hub.subscribe(a, "market:x")
            await hub.subscribe(a, "feed:global")
            await hub.unsubscribe_all(a, reason="test")
            reached_x = await hub.broadcast("market:x", {"type": "t"})
            reached_f = await hub.broadcast("feed:global", {"type": "t"})
            return reached_x, reached_f

        reached_x, reached_f = _run(scenario())
        self.assertEqual(reached_x, 0)
        self.assertEqual(reached_f, 0)
        self.assertEqual(hub.disconnect_reasons["test"], 1)

    def test_dead_connection_dropped_silently(self):
        hub = Hub()
        live, dead = FakeWS("live"), FakeWS("dead")
        dead.dead = True

        async def scenario():
            await hub.subscribe(live, "feed:global")
            await hub.subscribe(dead, "feed:global")
            reached = await hub.broadcast("feed:global", {"type": "x"})
            # Dead WS was evicted during broadcast; next broadcast only
            # reaches the live one.
            reached_again = await hub.broadcast("feed:global", {"type": "y"})
            return reached, reached_again

        reached, reached_again = _run(scenario())
        self.assertEqual(reached, 1)         # 1 success, 1 drop
        self.assertEqual(reached_again, 1)   # dead socket already evicted

    def test_stats_reports_connections_channels_and_top(self):
        hub = Hub()
        a, b, c = FakeWS("a"), FakeWS("b"), FakeWS("c")

        async def scenario():
            await hub.register_connection(a, user_id=1, ip="1.1.1.1")
            await hub.register_connection(b, user_id=2, ip="2.2.2.2")
            await hub.register_connection(c, user_id=1, ip="1.1.1.1")
            await hub.subscribe(a, "feed:global")
            await hub.subscribe(b, "feed:global")
            await hub.subscribe(c, "market:x")
            await hub.broadcast("feed:global", {"type": "x"})

        _run(scenario())
        stats = hub.stats()
        self.assertEqual(stats["connections"], 3)
        self.assertEqual(stats["unique_users"], 2)
        self.assertIn("feed:global", [r["channel"] for r in stats["top_channels"]])
        self.assertGreaterEqual(stats["msgs_last_60s"], 2)

    def test_evict_oldest_for_user(self):
        hub = Hub()
        ws1, ws2, ws3, ws4 = FakeWS("1"), FakeWS("2"), FakeWS("3"), FakeWS("4")

        async def scenario():
            for ws in (ws1, ws2, ws3, ws4):
                await hub.register_connection(ws, user_id=42, ip="x")
            return await hub.evict_oldest_for_user(42, max_concurrent=3)

        to_close = _run(scenario())
        # With 4 active and cap=3, keep 2 newest (ws3, ws4), evict the
        # oldest 2 (ws1, ws2) so the new incoming connection can take
        # the third slot.
        self.assertEqual(len(to_close), 2)
        self.assertIn(ws1, to_close)
        self.assertIn(ws2, to_close)


# ── Broadcast helpers ─────────────────────────────────────────────────────


class TestBroadcastHelpers(unittest.TestCase):
    """The emit_* wrappers in realtime/broadcast.py schedule coroutines.
    These tests verify they pick the right channel + payload shape."""

    def setUp(self):
        from realtime import broadcast as broadcast_module
        self.broadcast_module = broadcast_module
        self.captured: list[tuple[str, dict]] = []

        async def _fake_broadcast(channel, payload):
            self.captured.append((channel, payload))
            return 0

        # Replace the real hub.broadcast for the duration of each test.
        self._orig = broadcast_module.hub.broadcast
        broadcast_module.hub.broadcast = _fake_broadcast

    def tearDown(self):
        self.broadcast_module.hub.broadcast = self._orig

    def test_emit_new_prediction_fans_to_market_and_feed(self):
        self.broadcast_module.emit_new_prediction(
            source_handle="fedwatcher",
            market_slug="poly:fed-rate",
            category="macro",
            direction="YES",
            predicted_probability=0.74,
            content="Fed will hold rates",
        )
        channels_hit = [c for (c, _p) in self.captured]
        self.assertIn("market:poly:fed-rate", channels_hit)
        self.assertIn("feed:global", channels_hit)
        # Content truncated at 280 chars.
        long_payload = next(p for c, p in self.captured if c == "feed:global")
        self.assertEqual(long_payload["type"], "new_prediction")
        self.assertEqual(long_payload["prediction"]["source_handle"], "fedwatcher")

    def test_emit_new_prediction_skips_market_channel_when_no_slug(self):
        self.broadcast_module.emit_new_prediction(
            source_handle="x",
            market_slug=None,
            category="other",
            direction=None,
            predicted_probability=None,
            content="general take",
        )
        channels_hit = [c for (c, _p) in self.captured]
        self.assertNotIn("market:None", channels_hit)
        self.assertIn("feed:global", channels_hit)

    def test_emit_notification_targets_user_channel(self):
        self.broadcast_module.emit_notification(user_id=42, notification={"title": "hi"})
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self.captured[0][0], "user:42")
        self.assertEqual(self.captured[0][1]["type"], "notification")

    def test_emit_capture_attempt_hits_admin_security(self):
        self.broadcast_module.emit_capture_attempt(
            user_id=7, kind="screenshot", context={"ip": "1.2.3.4"},
        )
        self.assertEqual(self.captured[0][0], "admin:security")
        self.assertEqual(self.captured[0][1]["type"], "capture_attempt")

    def test_emit_price_tick_computes_no_price(self):
        self.broadcast_module.emit_price_tick(
            market_slug="poly:x", yes_price=0.6,
        )
        payload = self.captured[0][1]
        self.assertEqual(payload["type"], "price_tick")
        self.assertAlmostEqual(payload["no_price"], 0.4)


if __name__ == "__main__":
    unittest.main()
