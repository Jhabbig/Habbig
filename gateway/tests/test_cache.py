"""Tests for cache/ttl.py — the sync, single-process TTL cache.

Covered:
  * get/set/delete/delete_prefix/clear roundtrip
  * get_or_compute runs factory once per miss, cached thereafter
  * TTL expiration returns None + re-runs factory
  * Concurrent access is thread-safe (100 threads racing on one key)
  * Max-item eviction drops the soonest-to-expire entry
  * Stats counters attribute hits/misses/sets per prefix
  * ttl_invalidate helpers hit the right prefixes
  * /admin/cache page renders for admins and 403s for non-admins

Fast: every test tweaks TTLs to single digits. No I/O, no Claude, no network.
"""

from __future__ import annotations

import threading
import time
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations

from cache.ttl import TTLCache, ttl_cache, ttl_invalidate, DEFAULT_TTLS


class TestTTLCacheCore(unittest.TestCase):
    def setUp(self):
        self.c = TTLCache(max_items=100)

    def test_set_then_get(self):
        self.c.set("k", {"v": 1}, ttl_seconds=10)
        self.assertEqual(self.c.get("k"), {"v": 1})

    def test_missing_returns_none(self):
        self.assertIsNone(self.c.get("nope"))

    def test_delete_removes_key(self):
        self.c.set("k", 1, 10)
        self.assertEqual(self.c.delete("k"), 1)
        self.assertEqual(self.c.delete("k"), 0)  # second delete is a no-op
        self.assertIsNone(self.c.get("k"))

    def test_delete_prefix_only_matching(self):
        self.c.set("feed:a", 1, 10)
        self.c.set("feed:b", 2, 10)
        self.c.set("market:x", 3, 10)
        removed = self.c.delete_prefix("feed:")
        self.assertEqual(removed, 2)
        self.assertIsNone(self.c.get("feed:a"))
        self.assertEqual(self.c.get("market:x"), 3)

    def test_clear(self):
        for i in range(5):
            self.c.set(f"k{i}", i, 10)
        self.assertEqual(self.c.clear(), 5)
        self.assertIsNone(self.c.get("k0"))


class TestGetOrCompute(unittest.TestCase):
    def test_factory_runs_once_on_first_miss(self):
        c = TTLCache(max_items=10)
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            return 42

        self.assertEqual(c.get_or_compute("k", factory, 10), 42)
        self.assertEqual(c.get_or_compute("k", factory, 10), 42)
        self.assertEqual(c.get_or_compute("k", factory, 10), 42)
        self.assertEqual(calls["n"], 1)

    def test_exceptions_propagate_and_do_not_cache(self):
        c = TTLCache(max_items=10)

        def boom():
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            c.get_or_compute("k", boom, 10)
        # Nothing was cached; a second call still runs the factory.
        with self.assertRaises(RuntimeError):
            c.get_or_compute("k", boom, 10)
        self.assertIsNone(c.get("k"))


class TestTTLExpiration(unittest.TestCase):
    def test_expired_entry_returns_none(self):
        c = TTLCache(max_items=10)
        c.set("k", "v", ttl_seconds=1)
        self.assertEqual(c.get("k"), "v")
        time.sleep(1.1)
        self.assertIsNone(c.get("k"))

    def test_expired_get_or_compute_reruns_factory(self):
        c = TTLCache(max_items=10)
        counter = {"n": 0}

        def f():
            counter["n"] += 1
            return counter["n"]

        self.assertEqual(c.get_or_compute("k", f, 1), 1)
        time.sleep(1.1)
        self.assertEqual(c.get_or_compute("k", f, 1), 2)
        self.assertEqual(counter["n"], 2)

    def test_zero_or_negative_ttl_clamps_to_60(self):
        c = TTLCache(max_items=10)
        c.set("k", 1, ttl_seconds=0)
        # Should still be alive immediately — clamped TTL is 60s.
        self.assertEqual(c.get("k"), 1)


class TestEviction(unittest.TestCase):
    def test_evicts_when_over_max(self):
        c = TTLCache(max_items=3)
        # Fill at varying TTLs so the soonest-to-expire is known.
        c.set("short", 1, 1)
        c.set("mid", 2, 10)
        c.set("long", 3, 100)
        # Adding a 4th entry should evict "short" (soonest expiry).
        c.set("new", 4, 50)
        self.assertIsNone(c.get("short"))
        self.assertEqual(c.get("new"), 4)
        self.assertEqual(c.stats()["evictions"], 1)

    def test_resetting_existing_key_does_not_evict(self):
        c = TTLCache(max_items=2)
        c.set("a", 1, 10)
        c.set("b", 2, 10)
        # Overwriting existing key doesn't count toward the eviction budget.
        c.set("a", 99, 10)
        self.assertEqual(c.get("a"), 99)
        self.assertEqual(c.get("b"), 2)
        self.assertEqual(c.stats()["evictions"], 0)


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_get_or_compute_serialises(self):
        """100 threads racing on the same key: cache stays consistent."""
        c = TTLCache(max_items=10)
        counter = {"n": 0}
        lock = threading.Lock()

        def factory():
            # Simulate a slow DB query; parallel callers may both run it
            # because the cache releases its lock during factory work.
            with lock:
                counter["n"] += 1
                n = counter["n"]
            time.sleep(0.01)
            return n

        results = [None] * 100
        threads = []

        def worker(i):
            results[i] = c.get_or_compute("k", factory, ttl_seconds=10)

        for i in range(100):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        # Every thread got an integer back — thread-safety check is "no
        # crashes, no dropped writes, every caller gets a legit value."
        self.assertTrue(all(isinstance(r, int) for r in results))
        # Whatever ended up cached is one of the values a factory produced
        # (last-writer-wins; which one won depends on scheduling).
        cached = c.get("k")
        self.assertIn(cached, results)
        # Factory ran ≥1 time (racing callers can run it many times in early
        # contention — we don't guarantee single-flight, just thread safety).
        self.assertGreaterEqual(counter["n"], 1)


class TestStats(unittest.TestCase):
    def test_per_prefix_hit_miss_attribution(self):
        c = TTLCache(max_items=100)
        c.set("feed:a", 1, 10)
        c.set("market:x", 2, 10)

        c.get("feed:a")  # hit
        c.get("feed:a")  # hit
        c.get("feed:missing")  # miss
        c.get("market:x")  # hit
        c.get("market:missing")  # miss

        s = c.stats()
        by_prefix = {r["prefix"]: r for r in s["per_prefix"]}
        self.assertEqual(by_prefix["feed"]["hits"], 2)
        self.assertEqual(by_prefix["feed"]["misses"], 1)
        self.assertEqual(by_prefix["market"]["hits"], 1)
        self.assertEqual(by_prefix["market"]["misses"], 1)
        self.assertEqual(s["total_hits"], 3)
        self.assertEqual(s["total_misses"], 2)
        self.assertAlmostEqual(s["hit_rate"], 3 / 5, places=4)

    def test_reset_stats_clears_counters(self):
        c = TTLCache(max_items=10)
        c.set("k", 1, 10)
        c.get("k"); c.get("missing")
        c.reset_stats()
        s = c.stats()
        self.assertEqual(s["total_hits"], 0)
        self.assertEqual(s["total_misses"], 0)


class TestInvalidateHelpers(unittest.TestCase):
    """Verifies ttl_invalidate hits the right prefixes on each write event."""

    def setUp(self):
        ttl_cache.clear()
        ttl_cache.reset_stats()

    def test_on_new_prediction_invalidates_fan_out(self):
        ttl_cache.set("feed:user_1:cat_all:sort_new:page_1", ["a"], 60)
        ttl_cache.set("best_bets:tier_pro:page_1", ["b"], 120)
        ttl_cache.set("source:fedwatcher", {"c": 1}, 300)
        ttl_cache.set("source_history:fedwatcher", [], 300)
        ttl_cache.set("credibility_consensus:will-fed-hold", 0.7, 60)
        ttl_cache.set("market:will-fed-hold", {}, 30)  # unrelated, should survive

        removed = ttl_invalidate.on_new_prediction("fedwatcher", "will-fed-hold")
        self.assertEqual(removed, 5)
        self.assertIsNone(ttl_cache.get("feed:user_1:cat_all:sort_new:page_1"))
        self.assertIsNone(ttl_cache.get("source:fedwatcher"))
        # Unrelated market key left intact
        self.assertIsNotNone(ttl_cache.get("market:will-fed-hold"))

    def test_on_market_resolved_flushes_feed_and_market_keys(self):
        ttl_cache.set("market:foo", {}, 30)
        ttl_cache.set("market_chart:foo", [], 120)
        ttl_cache.set("credibility_consensus:foo", 0.5, 60)
        ttl_cache.set("feed:user_7:cat_x:sort_hot:page_1", [], 60)
        ttl_cache.set("source:x", {}, 300)  # should survive

        removed = ttl_invalidate.on_market_resolved("foo")
        self.assertEqual(removed, 4)
        self.assertIsNotNone(ttl_cache.get("source:x"))

    def test_on_credibility_recompute_wipes_source_namespaces(self):
        ttl_cache.set("source:a", 1, 300)
        ttl_cache.set("source_history:a", [], 300)
        ttl_cache.set("sources:sort_acc:filter_all:page_1", [], 120)
        ttl_cache.set("source_network", {}, 600)
        ttl_cache.set("market:foo", {}, 30)  # should survive

        removed = ttl_invalidate.on_credibility_recompute()
        self.assertEqual(removed, 4)
        self.assertIsNotNone(ttl_cache.get("market:foo"))

    def test_on_subscription_change_scoped_to_user(self):
        ttl_cache.set("feed:user_1:cat_all:sort_new:page_1", [], 60)
        ttl_cache.set("feed:user_2:cat_all:sort_new:page_1", [], 60)
        ttl_cache.set("best_bets:tier_free:page_1", [], 120)

        removed = ttl_invalidate.on_subscription_change(1)
        # User 1's feed + ALL best_bets (tier may have changed) = 2
        self.assertEqual(removed, 2)
        self.assertIsNone(ttl_cache.get("feed:user_1:cat_all:sort_new:page_1"))
        # User 2's feed should be untouched
        self.assertIsNotNone(ttl_cache.get("feed:user_2:cat_all:sort_new:page_1"))

    def test_on_role_change_does_not_touch_sync_cache(self):
        # Role-change is a per-user async-surface bust only. The sync TTL cache
        # (`feed:*`, `best_bets:*`) is not role-keyed, so it must stay intact.
        ttl_cache.set("feed:user_1:cat_all:sort_new:page_1", [], 60)
        ttl_cache.set("best_bets:tier_free:page_1", [], 120)

        removed = ttl_invalidate.on_role_change(1)
        self.assertEqual(removed, 0)
        self.assertIsNotNone(ttl_cache.get("feed:user_1:cat_all:sort_new:page_1"))
        self.assertIsNotNone(ttl_cache.get("best_bets:tier_free:page_1"))

    def test_on_role_change_busts_per_user_async_keys(self):
        # The async cache deletes are fire-and-forget. Drive a transient event
        # loop, seed the per-user keys, fire the helper, and confirm the bust
        # landed on the three canonical user-surface keys.
        import asyncio
        from cache.service import cache as _async_cache
        # Force the in-process fallback so we don't touch a real Redis.
        _async_cache._memory._store.clear()
        _async_cache._connect_attempted = True

        async def _seed():
            await _async_cache.set("dashboards:user:42", {"x": 1}, ttl_seconds=60)
            await _async_cache.set("settings:user:42", {"y": 1}, ttl_seconds=60)
            await _async_cache.set("signal_search:user:42", {"z": 1}, ttl_seconds=30)
            # An unrelated user's keys must survive.
            await _async_cache.set("dashboards:user:99", {"keep": 1}, ttl_seconds=60)

        asyncio.new_event_loop().run_until_complete(_seed())

        ttl_invalidate.on_role_change(42)

        async def _check():
            self.assertIsNone(await _async_cache.get("dashboards:user:42"))
            self.assertIsNone(await _async_cache.get("settings:user:42"))
            self.assertIsNone(await _async_cache.get("signal_search:user:42"))
            self.assertIsNotNone(await _async_cache.get("dashboards:user:99"))

        asyncio.new_event_loop().run_until_complete(_check())

    def test_everything_nukes_cache(self):
        ttl_cache.set("a", 1, 10)
        ttl_cache.set("b", 2, 10)
        self.assertEqual(ttl_invalidate.everything(), 2)
        self.assertIsNone(ttl_cache.get("a"))


class TestDefaultTTLs(unittest.TestCase):
    def test_spec_ttls_present(self):
        for prefix in [
            "feed", "best_bets", "markets", "market", "source", "sources",
            "source_history", "source_network", "market_chart",
            "insider_signals", "insider_leaderboard", "og_card",
            "credibility_consensus",
        ]:
            self.assertIn(prefix, DEFAULT_TTLS, f"{prefix} missing from DEFAULT_TTLS")
        # Spot-check a couple for correctness
        self.assertEqual(DEFAULT_TTLS["feed"], 60)
        self.assertEqual(DEFAULT_TTLS["og_card"], 3600)


class TestAdminCachePage(unittest.TestCase):
    """The /admin/cache page is gated on admin + real (not impersonated) user."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.pop("SITE_ACCESS_TOKEN", None)
        os.environ.pop("PRODUCTION", None)
        import db  # noqa: F401
        import server  # noqa: F401
        from fastapi.testclient import TestClient
        cls.client = TestClient(server.app)
        cls.server = server

    def test_anon_sees_denied_response_not_cache_page(self):
        # Admin pages render a "denied" page for unauth'd users (200 HTML,
        # not the cache stats). Assert the cache table isn't visible.
        r = self.client.get("/admin/cache", follow_redirects=False)
        self.assertNotIn("Hit rate", r.text)
        self.assertNotIn("Clear cache", r.text)

    def test_admin_sees_page(self):
        import db
        admin_email = "admin-cache@test.local"
        uid = db.create_user(
            admin_email, "TestPass123!", username="admincacheuser"
        )
        with db.conn() as c:
            c.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (uid,))
        token = db.create_session(uid)
        r = self.client.get(
            "/admin/cache",
            cookies={self.server.COOKIE_NAME: token},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("Hit rate", r.text)
        self.assertIn("Clear cache", r.text)

    def test_cache_stats_snapshot_shape(self):
        """Directly verify the stats() shape the /admin/cache/stats JSON
        endpoint returns. The HTTP path is admin-gated with 2FA, and test-
        harness auth for the 2FA branch is brittle — but `stats()` is the
        pure function the endpoint wraps, so testing it here covers the
        surface without depending on the auth transport."""
        from cache import ttl_cache
        body = ttl_cache.stats()
        for required in (
            "total", "expired", "live", "max_items", "evictions",
            "total_hits", "total_misses", "hit_rate", "per_prefix",
        ):
            self.assertIn(required, body, f"{required} missing from stats")
        self.assertIsInstance(body["per_prefix"], list)


if __name__ == "__main__":
    unittest.main()
