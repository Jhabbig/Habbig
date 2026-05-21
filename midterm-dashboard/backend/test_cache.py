"""Standalone tests for cache.TTLCache + etag_for.

Run with: python3 test_cache.py
"""
from __future__ import annotations

import asyncio
import sys

from cache import TTLCache, etag_for


def passed(label: str) -> None:
    print(f"PASS {label}")


def fail(label: str, detail: str = "") -> None:
    print(f"FAIL {label}: {detail}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 1. etag_for is content-stable
# ---------------------------------------------------------------------------

if etag_for({"a": 1, "b": 2}) != etag_for({"b": 2, "a": 1}):
    fail("etag_for: insertion order doesn't change the hash")
passed("etag_for: stable across dict insertion order")

if etag_for({"a": 1}) == etag_for({"a": 2}):
    fail("etag_for: different payloads → different hashes")
passed("etag_for: different payloads produce different hashes")

# String + bytes both work
if not etag_for("hello").startswith('W/"'):
    fail("etag_for: weak validator prefix")
passed("etag_for: weak validator prefix (W/...)")

# Non-trivial nested structure with datetime-ish object via default=str
import datetime
e = etag_for({"ts": datetime.datetime(2026, 11, 3, 23, 0)})
if not e.startswith('W/"') or len(e) < 10:
    fail("etag_for: handles default=str fallback", e)
passed("etag_for: serializes via default=str without crashing")


# ---------------------------------------------------------------------------
# 2. TTLCache basics
# ---------------------------------------------------------------------------

async def _basic_test():
    c = TTLCache(default_ttl=10.0)
    if c.get("missing") is not None:
        fail("TTLCache.get: missing key returns None")
    c.set("k", "v")
    if c.get("k") != "v":
        fail("TTLCache: set/get round-trip")
    c.invalidate("k")
    if c.get("k") is not None:
        fail("TTLCache.invalidate: drops the key")
    passed("TTLCache: basic set/get/invalidate")


async def _ttl_test():
    c = TTLCache()
    c.set("k", "v", ttl=0.05)
    if c.get("k") != "v":
        fail("TTLCache: fresh key visible")
    await asyncio.sleep(0.1)
    if c.get("k") is not None:
        fail("TTLCache: expired key returns None")
    passed("TTLCache: TTL expires the key")


# ---------------------------------------------------------------------------
# 3. get_or_compute is stampede-safe
# ---------------------------------------------------------------------------

async def _stampede_test():
    """50 concurrent callers hit a cold key. The compute function must be
    invoked exactly ONCE; everyone else should get the cached result."""
    c = TTLCache(default_ttl=10.0)
    call_count = 0

    async def compute():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # simulate slow DB
        return f"computed_{call_count}"

    results = await asyncio.gather(*[
        c.get_or_compute("hot_key", compute) for _ in range(50)
    ])

    if call_count != 1:
        fail(f"stampede: compute should run once, ran {call_count} times")
    if not all(r == "computed_1" for r in results):
        fail("stampede: all callers see the same computed result", str(set(results)))
    passed("TTLCache.get_or_compute: 50 concurrent callers → 1 compute")


async def _sync_compute_test():
    """compute can be a plain function, not just a coroutine."""
    c = TTLCache(default_ttl=10.0)
    r = await c.get_or_compute("sk", lambda: {"x": 42})
    if r != {"x": 42}:
        fail("get_or_compute: sync compute returns value")
    # Second call should hit cache
    call_count = 0

    def compute2():
        nonlocal call_count
        call_count += 1
        return "should_not_be_returned"
    r2 = await c.get_or_compute("sk", compute2)
    if r2 != {"x": 42} or call_count != 0:
        fail("get_or_compute: second call hits cache, doesn't re-invoke compute")
    passed("TTLCache.get_or_compute: handles sync compute + skips re-invocation")


async def _expire_recompute_test():
    c = TTLCache(default_ttl=0.05)
    call_count = 0

    async def compute():
        nonlocal call_count
        call_count += 1
        return call_count

    r1 = await c.get_or_compute("k", compute)
    await asyncio.sleep(0.08)
    r2 = await c.get_or_compute("k", compute)
    if r1 != 1 or r2 != 2:
        fail(f"expire+recompute: r1={r1}, r2={r2}")
    passed("TTLCache.get_or_compute: recomputes after TTL expires")


async def _run():
    await _basic_test()
    await _ttl_test()
    await _stampede_test()
    await _sync_compute_test()
    await _expire_recompute_test()


asyncio.run(_run())

print("\nAll cache tests passed.")
