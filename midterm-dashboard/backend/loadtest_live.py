"""Load test for /data/live/dashboard.

Fires N concurrent clients, each polling the endpoint every ``--interval``
seconds for ``--duration`` seconds. Reports request count, error rate,
and p50/p95/p99 latencies. Also reports the ratio of 304 Not Modified
responses (proves the ETag short-circuit is working under load).

Usage:
  # Local dev server on :8051, 100 clients polling every 15s for 60s
  python3 loadtest_live.py --base http://localhost:8051 \\
                           --clients 100 --interval 15 --duration 60

  # 1000 concurrent clients, no polling — pure burst
  python3 loadtest_live.py --base http://localhost:8051 \\
                           --clients 1000 --interval 0 --duration 5
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import aiohttp


async def _client_loop(
    client_id: int, session: aiohttp.ClientSession, base: str,
    interval: float, deadline: float, latencies: list[float],
    statuses: dict[int, int], errors: list[str],
    use_etag: bool,
) -> None:
    """One simulated user. Polls /data/live/dashboard until deadline."""
    etag: str | None = None
    while time.monotonic() < deadline:
        headers = {}
        if use_etag and etag:
            headers["If-None-Match"] = etag
        start = time.monotonic()
        try:
            async with session.get(
                f"{base}/data/live/dashboard",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                # Drain the body so the connection releases properly
                if r.status == 200:
                    await r.read()
                    etag = r.headers.get("etag") or etag
                elif r.status == 304:
                    # No body to read — but record latency anyway
                    pass
                else:
                    await r.read()
                statuses[r.status] = statuses.get(r.status, 0) + 1
                latencies.append(time.monotonic() - start)
        except asyncio.TimeoutError:
            errors.append(f"client {client_id}: timeout")
        except aiohttp.ClientError as e:
            errors.append(f"client {client_id}: {type(e).__name__} {e}")
        if interval > 0:
            await asyncio.sleep(interval)
        else:
            return  # burst mode — one request per client


async def main_async(args: argparse.Namespace) -> int:
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    errors: list[str] = []
    deadline = time.monotonic() + max(0.001, args.duration)

    connector = aiohttp.TCPConnector(limit=args.clients * 2)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            asyncio.create_task(_client_loop(
                i, session, args.base.rstrip("/"),
                args.interval, deadline, latencies, statuses, errors,
                use_etag=not args.no_etag,
            ))
            for i in range(args.clients)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    total = sum(statuses.values())
    if not latencies:
        print("FAIL: no requests completed")
        print("errors:", errors[:5])
        return 1

    latencies.sort()
    p = lambda q: latencies[min(len(latencies) - 1, int(len(latencies) * q))]
    print()
    print(f"=== /data/live/dashboard load test ===")
    print(f"clients: {args.clients}  interval: {args.interval}s  duration: {args.duration}s")
    print(f"ETag conditional GETs: {'OFF' if args.no_etag else 'ON'}")
    print(f"target: {args.base}")
    print()
    print(f"total requests:  {total}")
    print(f"errors:          {len(errors)}  ({100 * len(errors) / max(1, total + len(errors)):.2f}%)")
    print(f"by status:       {dict(sorted(statuses.items()))}")
    if 200 in statuses and 304 in statuses:
        bw_saved_pct = 100 * statuses[304] / (statuses[200] + statuses[304])
        print(f"304 rate:        {bw_saved_pct:.1f}% of responses (= bandwidth + serialization saved)")
    print()
    print(f"latency (s):")
    print(f"  min:           {latencies[0]:.4f}")
    print(f"  p50:           {p(0.50):.4f}")
    print(f"  p95:           {p(0.95):.4f}")
    print(f"  p99:           {p(0.99):.4f}")
    print(f"  max:           {latencies[-1]:.4f}")
    print(f"  mean:          {statistics.mean(latencies):.4f}")

    if errors:
        print()
        print("first 5 errors:")
        for e in errors[:5]:
            print(f"  {e}")

    # Pass/fail signal so CI can wire this up: zero errors, p95 < 2s
    p95 = p(0.95)
    if errors:
        print(f"\nFAIL: {len(errors)} errors")
        return 1
    if p95 > 2.0:
        print(f"\nFAIL: p95 {p95:.3f}s > 2s SLO")
        return 1
    print("\nOK: zero errors, p95 within SLO")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test /data/live/dashboard")
    parser.add_argument("--base", default="http://localhost:8051", help="server base URL")
    parser.add_argument("--clients", type=int, default=100, help="concurrent simulated users")
    parser.add_argument("--interval", type=float, default=15.0,
                        help="seconds between polls per client (0 = burst, one request each)")
    parser.add_argument("--duration", type=float, default=30.0, help="how long to run")
    parser.add_argument("--no-etag", action="store_true",
                        help="disable If-None-Match (forces full responses)")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
