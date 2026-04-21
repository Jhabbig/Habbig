#!/usr/bin/env python3
"""Micro-benchmark for the cache service.

Measures raw get/set overhead against the in-process backend (what a
typical cache hit does on a request). Useful for sanity-checking that
the JSON serialise / key-prefix / stats-recording overhead is in the
microseconds, not the milliseconds.

Usage:
    python3 scripts/bench_cache.py
    REDIS_URL=redis://localhost python3 scripts/bench_cache.py  # compare

For the integration-level benchmark (endpoint before/after caching),
run locally: hit each wired endpoint 100× fresh (cold) then 100× more
(warm) and compare median response time from `observability.perf_stats`
via /admin/api/performance.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cache.service import CacheService


async def _bench_get_miss(svc: CacheService, n: int) -> list[float]:
    times: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        await svc.get(f"missing_key_{i}")
        times.append(time.perf_counter() - t0)
    return times


async def _bench_set(svc: CacheService, n: int) -> list[float]:
    times: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        await svc.set(f"bench_{i}", {"i": i, "data": "x" * 200}, 60)
        times.append(time.perf_counter() - t0)
    return times


async def _bench_get_hit(svc: CacheService, n: int) -> list[float]:
    # Pre-populate so every iteration hits.
    for i in range(n):
        await svc.set(f"hit_{i}", {"i": i, "data": "x" * 200}, 60)
    times: list[float] = []
    for i in range(n):
        t0 = time.perf_counter()
        await svc.get(f"hit_{i}")
        times.append(time.perf_counter() - t0)
    return times


def _report(label: str, samples: list[float]) -> None:
    med_us = statistics.median(samples) * 1e6
    p95_us = sorted(samples)[int(len(samples) * 0.95)] * 1e6
    mean_us = statistics.mean(samples) * 1e6
    print(
        f"  {label:14s}  n={len(samples):5d}  "
        f"mean={mean_us:7.1f}µs  median={med_us:7.1f}µs  p95={p95_us:7.1f}µs"
    )


async def main() -> None:
    svc = CacheService()
    backend = svc.stats()["backend"]
    redis_url = os.environ.get("REDIS_URL", "") or "(unset)"
    print(f"Cache backend: {backend!s}  REDIS_URL={redis_url}")

    n = 5000
    print(f"Running {n} iterations per op …")
    miss_times = await _bench_get_miss(svc, n)
    set_times = await _bench_set(svc, n)
    hit_times = await _bench_get_hit(svc, n)

    _report("get (miss)", miss_times)
    _report("set", set_times)
    _report("get (hit)", hit_times)

    stats = svc.stats()
    print(
        f"Stats: hits={stats['hits']} misses={stats['misses']} "
        f"sets={stats['sets']} errors={stats['errors']} "
        f"memory_size={stats['memory_size']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
