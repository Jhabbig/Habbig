#!/usr/bin/env python3
"""Before/after latency benchmark for narve.ai hot endpoints.

Runs N serial GET requests against a target base URL, records timings,
and prints P50/P95/P99 per endpoint. Designed to be run against both a
pre-080 baseline and a post-080 build so the effect of the index +
tracer changes is a single diff away.

Usage::

    # Baseline (checked out at parent of this branch):
    python3 gateway/scripts/benchmark_endpoints.py --base http://localhost:7000 \\
            --session "$NARVE_SESSION_TOKEN" --runs 100 > /tmp/bench_before.txt

    # After applying migration 080:
    python3 gateway/scripts/benchmark_endpoints.py --base http://localhost:7000 \\
            --session "$NARVE_SESSION_TOKEN" --runs 100 > /tmp/bench_after.txt

    diff -u /tmp/bench_before.txt /tmp/bench_after.txt

Notes:
  * Only stdlib — no requests, no httpx. Runs on any server that has
    Python without a venv.
  * Hits a fixed panel of endpoints listed in ENDPOINTS below. Append
    more by editing the tuple; do NOT use CLI overrides — we want the
    baseline/after comparisons to be apples-to-apples.
  * Uses serial requests on purpose: N+1 fixes and index adds help
    p-tail latency under real load, but a parallel flood mixes server
    contention into the numbers and makes the diff noisy. 100 serial
    requests is enough to produce stable P95s on the target box.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import List, Tuple


ENDPOINTS: Tuple[Tuple[str, str], ...] = (
    # (label, path)
    ("feed",       "/api/feed"),
    ("best-bets",  "/api/best-bets"),
    ("sources",    "/api/sources"),
    # Sources detail is parameterized; caller can override via --handle.
    ("source-detail", "/api/sources/{handle}"),
    ("markets",    "/api/markets"),
    # Markets detail is parameterized; caller can override via --slug.
    ("market-detail", "/api/markets/{slug}"),
)


def _percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    vs = sorted(values)
    k = (len(vs) - 1) * q
    f = int(k)
    c_idx = min(f + 1, len(vs) - 1)
    return vs[f] + (vs[c_idx] - vs[f]) * (k - f)


def _hit_once(url: str, session: str, timeout: float) -> Tuple[float, int, int]:
    """Return (duration_ms, status, response_bytes). Failures count as
    their own latency sample + status code — we don't filter them so a
    regression that replaces 200s with 500s shows up in the aggregate."""
    req = urllib.request.Request(url)
    if session:
        req.add_header("Cookie", f"narve_session={session}")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return elapsed_ms, resp.status, len(body)
    except urllib.error.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return elapsed_ms, exc.code, 0
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return elapsed_ms, -1, 0


def run(base: str, session: str, runs: int, timeout: float,
        handle: str, slug: str) -> dict:
    """Execute every endpoint ``runs`` times serially. Returns a summary
    dict suitable for JSON dump + text rendering."""
    results: dict = {
        "base": base,
        "runs_per_endpoint": runs,
        "endpoints": [],
    }
    for label, path_template in ENDPOINTS:
        path = (
            path_template
            .replace("{handle}", handle)
            .replace("{slug}", slug)
        )
        url = base.rstrip("/") + path
        timings: List[float] = []
        statuses: List[int] = []
        for _ in range(runs):
            ms, status, _size = _hit_once(url, session, timeout)
            timings.append(ms)
            statuses.append(status)
        ok = sum(1 for s in statuses if 200 <= s < 300)
        summary = {
            "label": label,
            "url": url,
            "runs": runs,
            "ok_count": ok,
            "p50_ms": round(_percentile(timings, 0.50), 1),
            "p95_ms": round(_percentile(timings, 0.95), 1),
            "p99_ms": round(_percentile(timings, 0.99), 1),
            "max_ms": round(max(timings), 1) if timings else 0.0,
            "mean_ms": round(statistics.fmean(timings), 1) if timings else 0.0,
        }
        results["endpoints"].append(summary)
    return results


def _print_human(summary: dict) -> None:
    print(f"# narve benchmark — base={summary['base']}  runs={summary['runs_per_endpoint']}")
    print(f"{'endpoint':<18}{'ok':>5}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}{'mean':>9}")
    print("-" * 68)
    for e in summary["endpoints"]:
        print(
            f"{e['label']:<18}{e['ok_count']:>5}"
            f"{e['p50_ms']:>9.1f}{e['p95_ms']:>9.1f}"
            f"{e['p99_ms']:>9.1f}{e['max_ms']:>9.1f}"
            f"{e['mean_ms']:>9.1f}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:7000",
                    help="gateway base URL (default: %(default)s)")
    ap.add_argument("--session", default="",
                    help="narve_session cookie value for authenticated endpoints")
    ap.add_argument("--runs", type=int, default=100,
                    help="requests per endpoint (default: %(default)s)")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="per-request timeout seconds (default: %(default)s)")
    ap.add_argument("--handle", default="PolymarketAnalytics",
                    help="handle to substitute into /api/sources/{handle}")
    ap.add_argument("--slug", default="",
                    help="slug to substitute into /api/markets/{slug}; "
                         "empty = skip market-detail")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of text table")
    args = ap.parse_args()

    summary = run(
        base=args.base,
        session=args.session,
        runs=args.runs,
        timeout=args.timeout,
        handle=args.handle,
        slug=args.slug or "",
    )
    if args.json:
        json.dump(summary, sys.stdout, indent=2)
        print()
    else:
        _print_human(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
