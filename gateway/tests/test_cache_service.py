"""Unit tests for cache/service.py — the in-process fallback path.

These tests run without Redis. The Redis branch is exercised in a
separate integration test (skipped when REDIS_URL is unset) because
pulling in fakeredis would add a heavy dep we don't want for the unit
suite.

We reset the singleton's stats between tests and install a fresh
`_MemoryBackend` so each test starts clean.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cache.service import (  # noqa: E402
    CacheService,
    _MemoryBackend,
    _make_key,
    CACHE_KEY_PREFIX,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestMemoryBackend(unittest.TestCase):
    """Unit tests for the in-process TTL-aware dict backing store."""

    def test_get_missing_returns_none(self):
        b = _MemoryBackend()
        self.assertIsNone(b.get("nope"))

    def test_set_then_get(self):
        b = _MemoryBackend()
        b.set("k", '"v"', 60)
        self.assertEqual(b.get("k"), '"v"')

    def test_ttl_zero_still_returned(self):
        # ttl<=0 is clamped by CacheService; _MemoryBackend.set with 0
        # means "no expiry" in the backing store.
        b = _MemoryBackend()
        b.set("k", "x", 0)
        self.assertEqual(b.get("k"), "x")

    def test_ttl_expires(self):
        b = _MemoryBackend()
        b.set("k", "x", 1)
        # Fast-forward time using a fake monotonic source isn't worth the
        # fixture weight — sleep a hair past the TTL instead.
        time.sleep(1.1)
        self.assertIsNone(b.get("k"))

    def test_delete_returns_count(self):
        b = _MemoryBackend()
        b.set("k", "x", 60)
        self.assertEqual(b.delete("k"), 1)
        self.assertEqual(b.delete("k"), 0)

    def test_delete_pattern_glob(self):
        b = _MemoryBackend()
        b.set("a:1", "x", 60)
        b.set("a:2", "x", 60)
        b.set("b:1", "x", 60)
        self.assertEqual(b.delete_pattern("a:*"), 2)
        self.assertIsNone(b.get("a:1"))
        self.assertIsNone(b.get("a:2"))
        self.assertEqual(b.get("b:1"), "x")

    def test_size(self):
        b = _MemoryBackend()
        b.set("x", "1", 60)
        b.set("y", "2", 60)
        self.assertEqual(b.size(), 2)


class TestCacheService(unittest.TestCase):
    """End-to-end tests of the async wrapper (in-process backend only)."""

    def setUp(self):
        # Force Redis off so every test stays in the in-process branch.
        os.environ.pop("REDIS_URL", None)
        self.svc = CacheService()
        # _ensure_connected() short-circuits when the URL is empty.
        self.svc._connect_attempted = True

    def test_get_miss(self):
        self.assertIsNone(_run(self.svc.get("nope")))
        self.assertEqual(self.svc.stats()["misses"], 1)
        self.assertEqual(self.svc.stats()["hits"], 0)

    def test_set_then_get(self):
        _run(self.svc.set("k", {"x": 1}, ttl_seconds=60))
        self.assertEqual(_run(self.svc.get("k")), {"x": 1})
        stats = self.svc.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["sets"], 1)

    def test_key_is_prefixed(self):
        _run(self.svc.set("k", 1, ttl_seconds=60))
        # Raw in-memory store uses the prefixed key.
        self.assertIn(_make_key("k"), self.svc._memory._store)
        self.assertTrue(_make_key("k").startswith(CACHE_KEY_PREFIX))

    def test_delete(self):
        _run(self.svc.set("k", 1, ttl_seconds=60))
        self.assertEqual(_run(self.svc.delete("k")), 1)
        self.assertIsNone(_run(self.svc.get("k")))

    def test_delete_pattern(self):
        _run(self.svc.set("source:a", 1, 60))
        _run(self.svc.set("source:b", 2, 60))
        _run(self.svc.set("other:a", 3, 60))
        removed = _run(self.svc.delete_pattern("source:*"))
        self.assertEqual(removed, 2)
        self.assertIsNone(_run(self.svc.get("source:a")))
        self.assertEqual(_run(self.svc.get("other:a")), 3)

    def test_get_or_set_misses_then_hits(self):
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            return {"val": calls["n"]}

        # First call misses → factory runs.
        first = _run(self.svc.get_or_set("k", factory, ttl_seconds=60))
        self.assertEqual(first, {"val": 1})
        # Second call hits → factory must NOT run again.
        second = _run(self.svc.get_or_set("k", factory, ttl_seconds=60))
        self.assertEqual(second, {"val": 1})
        self.assertEqual(calls["n"], 1)

    def test_ttl_clamp(self):
        # ttl_seconds <= 0 is clamped to 60 so the memory store doesn't
        # accumulate forever.
        _run(self.svc.set("k", "v", ttl_seconds=0))
        entry = self.svc._memory._store[_make_key("k")]
        expires_at, _ = entry
        self.assertGreater(expires_at, time.time())

    def test_ttl_expires(self):
        _run(self.svc.set("k", "v", ttl_seconds=1))
        time.sleep(1.1)
        self.assertIsNone(_run(self.svc.get("k")))

    def test_unserialisable_value_does_not_raise(self):
        class _X:
            pass

        # Should log and skip, not raise.
        _run(self.svc.set("k", _X(), ttl_seconds=60))
        self.assertIsNone(_run(self.svc.get("k")))
        self.assertGreater(self.svc.stats()["errors"], 0)

    def test_datetime_serialises_via_default_str(self):
        import datetime as _dt
        now = _dt.datetime(2026, 1, 1, 12, 0)
        _run(self.svc.set("k", {"at": now}, ttl_seconds=60))
        # default=str converts — the round-trip value is the string form.
        got = _run(self.svc.get("k"))
        self.assertEqual(got, {"at": "2026-01-01 12:00:00"})

    def test_corrupt_json_treated_as_miss(self):
        # Simulate a poisoned cache entry. Must not raise, and should
        # return None (miss).
        self.svc._memory._store[_make_key("k")] = (time.time() + 60, "{not-json")
        self.assertIsNone(_run(self.svc.get("k")))
        self.assertGreater(self.svc.stats()["errors"], 0)

    def test_disabled_by_env(self):
        os.environ["CACHE_ENABLED"] = "false"
        try:
            svc = CacheService()
            svc._connect_attempted = True
            _run(svc.set("k", "v", 60))
            self.assertIsNone(_run(svc.get("k")))
        finally:
            os.environ.pop("CACHE_ENABLED", None)

    def test_stats_fields(self):
        _run(self.svc.set("k", 1, 60))
        _run(self.svc.get("k"))
        _run(self.svc.get("miss"))
        stats = self.svc.stats()
        self.assertEqual(stats["backend"], "memory")
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["sets"], 1)
        self.assertAlmostEqual(stats["hit_rate"], 0.5)

    def test_reset_stats(self):
        _run(self.svc.set("k", 1, 60))
        _run(self.svc.get("k"))
        self.svc.reset_stats()
        stats = self.svc.stats()
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)
        self.assertEqual(stats["sets"], 0)


if __name__ == "__main__":
    unittest.main()
