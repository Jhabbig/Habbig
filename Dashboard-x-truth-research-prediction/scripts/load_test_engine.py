#!/usr/bin/env python3
"""Load test for the Stage-3 Prediction Engine.

Proves the concurrency requirement: N concurrent users (default 500, duty
cycle ~0.3) sustained against the fusion layer, with a p95 latency assertion.
Two modes:

  In-process (default) — drives PredictionEngine.predict directly on an
  isolated temp SQLite DB. Measures the engine itself with no HTTP overhead.

      python scripts/load_test_engine.py --concurrency 500 --jobs 5000

  HTTP — drives a running server end-to-end (auth included):

      python scripts/load_test_engine.py --url http://127.0.0.1:18789 \
          --api-key narve_... --concurrency 200 --jobs 2000

Reports throughput, p50/p95/p99, cache hit rate, degraded rate; exits nonzero
if p95 exceeds --p95-target-ms (default 150ms in-process / 500ms HTTP).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `app` importable


import argparse
import asyncio
import os
import random
import tempfile
import time
import uuid


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--concurrency", type=int, default=500, help="peak concurrent users")
    p.add_argument("--jobs", type=int, default=5000, help="total jobs to run")
    p.add_argument("--duty", type=float, default=0.3, help="avg duty cycle (think-time model)")
    p.add_argument("--unique-inputs", type=int, default=500,
                   help="distinct job contents (smaller = more dedup-cache hits, like production)")
    p.add_argument("--p95-target-ms", type=float, default=None)
    p.add_argument("--url", default=None, help="HTTP mode: base URL of a running server")
    p.add_argument("--api-key", default=os.environ.get("ENGINE_API_KEY", ""))
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def make_job_payload(rng: random.Random, content_id: int) -> dict:
    """Synthetic but shape-faithful job. content_id pins the dedupable content."""
    content_rng = random.Random(content_id)  # content is a pure function of the id
    return {
        "job_id": str(uuid.UUID(int=rng.getrandbits(128))),
        "user_id": f"user-{rng.randrange(10000)}",
        "job_class": "interactive",
        "credibility": [
            {"post_id": f"x:{content_id}:{i}", "source": "x" if i % 2 == 0 else "reddit",
             "score": round(content_rng.uniform(0.2, 0.9), 3), "features": {}}
            for i in range(3)
        ],
        "metrics": {
            "predicted": {"predicted_probability": round(content_rng.uniform(0.1, 0.9), 3)},
            "extracted": {"engagement_velocity": content_rng.randrange(0, 1000)},
            "model_version": "claude-haiku-4-5",
            "usage": {"input_tokens": 1200, "output_tokens": 150, "cache_read_input_tokens": 900},
        },
        "context": {
            "market_slug": f"market-{content_id % 50}",
            "market_implied_probability": round(content_rng.uniform(0.05, 0.95), 3),
            "category": "politics",
        },
    }


async def run_inprocess(args: argparse.Namespace, latencies: list) -> dict:
    tmp = tempfile.mkdtemp(prefix="engine-loadtest-")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/loadtest.db"

    from sqlmodel import SQLModel
    import app.db as db
    from app.engine.schemas import EngineJob
    from app.engine.service import PredictionEngine

    async with db.engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    engine = PredictionEngine()
    rng = random.Random(args.seed)
    payloads = [make_job_payload(rng, rng.randrange(args.unique_inputs)) for _ in range(args.jobs)]
    queue: asyncio.Queue = asyncio.Queue()
    for payload in payloads:
        queue.put_nowait(payload)

    think_time = (1.0 - args.duty) * 0.01  # scaled-down think time keeps runtime sane

    async def worker() -> None:
        while True:
            try:
                payload = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            t0 = time.perf_counter()
            await engine.predict(EngineJob(**payload))
            latencies.append((time.perf_counter() - t0) * 1000)
            if think_time:
                await asyncio.sleep(random.uniform(0, 2 * think_time))

    t_start = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(args.concurrency)))
    elapsed = time.perf_counter() - t_start
    await engine.audit.flush()
    snapshot = engine.metrics.snapshot()
    snapshot["dedup_cache"] = engine.cache.stats()
    return {"elapsed_s": elapsed, "metrics": snapshot}


async def run_http(args: argparse.Namespace, latencies: list) -> dict:
    import httpx

    rng = random.Random(args.seed)
    payloads = [make_job_payload(rng, rng.randrange(args.unique_inputs)) for _ in range(args.jobs)]
    queue: asyncio.Queue = asyncio.Queue()
    for payload in payloads:
        queue.put_nowait(payload)
    headers = {"X-API-Key": args.api_key}
    errors = 0

    async def worker(client: httpx.AsyncClient) -> None:
        nonlocal errors
        while True:
            try:
                payload = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            t0 = time.perf_counter()
            r = await client.post(f"{args.url}/api/v1/engine/predict", json=payload, headers=headers)
            latencies.append((time.perf_counter() - t0) * 1000)
            if r.status_code != 200:
                errors += 1

    t_start = time.perf_counter()
    async with httpx.AsyncClient(timeout=30) as client:
        await asyncio.gather(*(worker(client) for _ in range(args.concurrency)))
    elapsed = time.perf_counter() - t_start

    async with httpx.AsyncClient(timeout=30) as client:
        metrics = (await client.get(f"{args.url}/api/v1/engine/metrics", headers=headers)).json()
    return {"elapsed_s": elapsed, "metrics": metrics, "errors": errors}


def percentile(values: list, q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))] if ordered else 0.0


def main() -> int:
    args = parse_args()
    p95_target = args.p95_target_ms or (500.0 if args.url else 150.0)
    latencies: list = []

    result = asyncio.run(run_http(args, latencies) if args.url else run_inprocess(args, latencies))

    p50, p95, p99 = percentile(latencies, 0.5), percentile(latencies, 0.95), percentile(latencies, 0.99)
    metrics = result["metrics"]
    print(f"\n=== Engine load test ({'HTTP' if args.url else 'in-process'}) ===")
    print(f"jobs={args.jobs} concurrency={args.concurrency} duty={args.duty} unique_inputs={args.unique_inputs}")
    print(f"elapsed: {result['elapsed_s']:.2f}s  throughput: {args.jobs / result['elapsed_s']:.0f} jobs/s")
    print(f"latency ms: p50={p50:.2f} p95={p95:.2f} p99={p99:.2f}")
    print(f"cache hit rate: {metrics.get('cache_hit_rate')}  degraded rate: {metrics.get('degraded_rate')}")
    print(f"cost/1k predictions: ${metrics.get('cost_per_1k_predictions_usd')}  alert: {metrics.get('cost_alert')}")
    if result.get("errors"):
        print(f"HTTP errors: {result['errors']}")
        return 1
    if p95 > p95_target:
        print(f"FAIL: p95 {p95:.2f}ms exceeds target {p95_target}ms")
        return 1
    print(f"PASS: p95 {p95:.2f}ms within target {p95_target}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
