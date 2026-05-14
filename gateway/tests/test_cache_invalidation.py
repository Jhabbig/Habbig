"""Tests for cache.invalidate.*: the named invalidation helpers used by
write paths. Verify that each helper targets exactly the key families it
claims and doesn't nuke unrelated entries.

These run against the in-process fallback with Redis disabled.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.pop("REDIS_URL", None)

from cache import cache, invalidate  # noqa: E402


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class TestInvalidate(unittest.TestCase):
    def setUp(self):
        # Reset the module-level singleton's stats + memory between tests.
        cache._memory._store.clear()
        cache.reset_stats()
        cache._connect_attempted = True  # stay in in-process branch

    def _seed(self, pairs):
        for k, v in pairs:
            _run(cache.set(k, v, ttl_seconds=600))

    def test_source_invalidates_per_source_keys(self):
        self._seed([
            ("credibility:sho", 1),
            ("credibility:sho:calibration", 2),
            ("source:sho", 3),
            ("source_calibration:sho", 4),
            ("source_history:sho", 5),
            ("source_history:sho:page_1", 6),
            ("credibility:julian", 7),  # unrelated
            ("sources:limit_20:offset_0", 8),  # unrelated (list cache)
        ])
        _run(invalidate.source("sho"))
        self.assertIsNone(_run(cache.get("credibility:sho")))
        self.assertIsNone(_run(cache.get("source:sho")))
        self.assertIsNone(_run(cache.get("source_calibration:sho")))
        self.assertIsNone(_run(cache.get("source_history:sho")))
        self.assertIsNone(_run(cache.get("source_history:sho:page_1")))
        # Unrelated entries must NOT be evicted.
        self.assertEqual(_run(cache.get("credibility:julian")), 7)
        self.assertEqual(_run(cache.get("sources:limit_20:offset_0")), 8)

    def test_all_sources_wipes_fan_out(self):
        self._seed([
            ("source:sho", 1),
            ("credibility:sho", 2),
            ("credibility:julian", 3),
            ("sources:limit_20:offset_0", 4),
            ("predictions:cat_politics", 5),
            ("market_probability:poly:trump", 6),
            ("v1_source:sho", 7),
            ("something_else", 99),  # unrelated key
        ])
        _run(invalidate.all_sources())
        for key in (
            "source:sho",
            "credibility:sho",
            "credibility:julian",
            "sources:limit_20:offset_0",
            "predictions:cat_politics",
            "market_probability:poly:trump",
        ):
            self.assertIsNone(_run(cache.get(key)), f"{key!r} should be gone")
        # Totally unrelated key survives.
        self.assertEqual(_run(cache.get("something_else")), 99)

    def test_market_invalidates_market_keys(self):
        self._seed([
            ("market_probability:poly:trump", 1),
            ("market_retrospective:poly:trump", 2),
            ("market:poly:trump", 3),
            ("market:poly:trump:variant", 4),
            ("market_probability:poly:biden", 5),  # unrelated market
            ("sources:limit_20:offset_0", 6),  # unrelated family
        ])
        _run(invalidate.market("poly:trump"))
        for k in (
            "market_probability:poly:trump",
            "market_retrospective:poly:trump",
            "market:poly:trump",
            "market:poly:trump:variant",
        ):
            self.assertIsNone(_run(cache.get(k)))
        self.assertEqual(_run(cache.get("market_probability:poly:biden")), 5)

    def test_environmental(self):
        self._seed([
            ("env_top:limit_20", 1),
            ("env_top:limit_50", 2),
            ("credibility:sho", 3),
        ])
        _run(invalidate.environmental())
        self.assertIsNone(_run(cache.get("env_top:limit_20")))
        self.assertIsNone(_run(cache.get("env_top:limit_50")))
        self.assertEqual(_run(cache.get("credibility:sho")), 3)

    def test_everything(self):
        self._seed([
            ("a", 1),
            ("b:nested", 2),
            ("c:1:2:3", 3),
        ])
        removed = _run(invalidate.everything())
        self.assertEqual(removed, 3)
        self.assertIsNone(_run(cache.get("a")))
        self.assertIsNone(_run(cache.get("b:nested")))
        self.assertIsNone(_run(cache.get("c:1:2:3")))


if __name__ == "__main__":
    unittest.main()
