#!/usr/bin/env python3
"""Large-data benchmarks for the narve gateway.

Seeds a throwaway SQLite DB with synthetic rows at realistic scale,
times each hot-read scenario, and fails CI if any p95 busts its
budget. See LARGE_DATA_BENCHMARKS.md at repo root for the scenario
table + what to do when a budget trips.

Usage:
    python3 gateway/scripts/bench_large_data.py
    python3 gateway/scripts/bench_large_data.py --scenario source_profile_detail
    python3 gateway/scripts/bench_large_data.py --iterations 50   # sample size

Output is one line per scenario plus a final summary. Exit 0 on all-OK,
1 on any bust. Safe to run locally or in CI.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable


# ── Seeding primitives ─────────────────────────────────────────────────────


def _create_schema(conn: sqlite3.Connection) -> None:
    """Minimum schema the scenarios touch. Not a full auth.db clone —
    just the tables the benchmarked queries hit. Kept in-file so the
    script stays runnable without the full migration chain."""
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            username TEXT UNIQUE NOT NULL,
            created_at INTEGER NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_handle TEXT NOT NULL,
            market_id TEXT,
            category TEXT,
            content TEXT,
            extracted_at INTEGER NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            resolved_correct INTEGER
        );
        CREATE INDEX idx_predictions_source ON predictions(source_handle);
        CREATE INDEX idx_predictions_source_resolved
            ON predictions(source_handle, resolved);
        CREATE INDEX idx_predictions_market_resolved
            ON predictions(market_id, resolved);
        CREATE INDEX idx_predictions_extracted_resolved
            ON predictions(extracted_at DESC, resolved);
        CREATE TABLE saved_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            prediction_id INTEGER NOT NULL,
            notes TEXT,
            saved_at INTEGER NOT NULL
        );
        CREATE INDEX idx_saved_user ON saved_predictions(user_id);
        CREATE TABLE takes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            market_slug TEXT NOT NULL,
            position TEXT NOT NULL,
            confidence REAL,
            reasoning TEXT,
            created_at INTEGER NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_takes_market ON takes(market_slug);
    """)


def _seed_users(conn: sqlite3.Connection, n: int) -> None:
    """Bulk-insert `n` users with a tight loop. Uses executemany for
    speed — 3 k rows in well under a second."""
    now = int(time.time())
    rows = [
        (f"u{i}@example.com", f"user{i}", now - i, 0)
        for i in range(n)
    ]
    conn.executemany(
        "INSERT INTO users (email, username, created_at, is_admin) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )


def _seed_source_predictions(
    conn: sqlite3.Connection, handle: str, n: int,
) -> None:
    """One source with `n` resolved + unresolved predictions."""
    now = int(time.time())
    rng = random.Random(42 + hash(handle) % 100)
    rows = []
    for i in range(n):
        resolved = i % 3 == 0   # ~33% resolved
        rows.append((
            handle,
            f"poly:mkt_{i // 10}",  # cluster into ~n/10 markets
            rng.choice(["politics", "crypto", "sports", "weather"]),
            f"prediction {i} about something",
            now - i,
            1 if resolved else 0,
            (1 if i % 2 else 0) if resolved else None,
        ))
    conn.executemany(
        "INSERT INTO predictions (source_handle, market_id, category, "
        "content, extracted_at, resolved, resolved_correct) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _seed_saved_for_user(
    conn: sqlite3.Connection, user_id: int, n: int,
) -> None:
    """`n` saved-prediction rows for a single user. Uses low-range
    prediction_ids to generate FK-shaped joins later."""
    now = int(time.time())
    rows = [
        (user_id, (i % 100) + 1, f"note {i}", now - i)
        for i in range(n)
    ]
    conn.executemany(
        "INSERT INTO saved_predictions (user_id, prediction_id, notes, saved_at) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )


def _seed_takes_on_market(
    conn: sqlite3.Connection, slug: str, n: int, user_count: int,
) -> None:
    """`n` takes on a hot market, spread across `user_count` users."""
    now = int(time.time())
    rows = [
        (
            (i % user_count) + 1,
            slug,
            "yes" if i % 2 else "no",
            0.5 + (i % 10) / 20.0,
            f"reasoning {i}",
            now - i,
            0,
        )
        for i in range(n)
    ]
    conn.executemany(
        "INSERT INTO takes (user_id, market_slug, position, confidence, "
        "reasoning, created_at, resolved) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


# ── Scenario registry ──────────────────────────────────────────────────────


@dataclasses.dataclass
class Scenario:
    name: str
    rows: int
    budget_ms_p95: float
    seed: Callable[[sqlite3.Connection], None]
    run: Callable[[sqlite3.Connection], None]


def _mk_scenarios() -> list[Scenario]:
    def seed_saved(conn):
        _seed_users(conn, 10)
        _seed_source_predictions(conn, "sho", 200)
        _seed_saved_for_user(conn, 1, 5000)

    def run_saved(conn):
        conn.execute(
            "SELECT sp.*, p.source_handle, p.category, p.content "
            "FROM saved_predictions sp "
            "LEFT JOIN predictions p ON p.id = sp.prediction_id "
            "WHERE sp.user_id = ? "
            "ORDER BY sp.saved_at DESC "
            "LIMIT 50 OFFSET 0",
            (1,),
        ).fetchall()

    def seed_source(conn):
        _seed_source_predictions(conn, "sho", 50_000)

    def run_source(conn):
        # Profile page: recent resolved predictions for the source.
        conn.execute(
            "SELECT * FROM predictions "
            "WHERE source_handle = ? AND resolved = 1 "
            "ORDER BY extracted_at DESC "
            "LIMIT 100",
            ("sho",),
        ).fetchall()
        # Plus an aggregate count.
        conn.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN resolved_correct = 1 THEN 1 ELSE 0 END) AS correct "
            "FROM predictions "
            "WHERE source_handle = ? AND resolved = 1",
            ("sho",),
        ).fetchone()

    def seed_market(conn):
        _seed_source_predictions(conn, "sho", 500)

    def run_market(conn):
        conn.execute(
            "SELECT * FROM predictions "
            "WHERE market_id = ? "
            "ORDER BY extracted_at DESC",
            ("poly:mkt_0",),
        ).fetchall()

    def seed_admin(conn):
        _seed_users(conn, 3000)

    def run_admin(conn):
        conn.execute(
            "SELECT id, username, email, is_admin, created_at "
            "FROM users ORDER BY created_at DESC LIMIT 200",
        ).fetchall()

    def seed_takes(conn):
        _seed_users(conn, 200)
        _seed_takes_on_market(conn, "trump-2026", 200, user_count=200)

    def run_takes(conn):
        conn.execute(
            "SELECT * FROM takes "
            "WHERE market_slug = ? "
            "ORDER BY created_at DESC",
            ("trump-2026",),
        ).fetchall()

    return [
        Scenario(
            name="user_saved_predictions_list",
            rows=5_000,
            budget_ms_p95=2000,
            seed=seed_saved,
            run=run_saved,
        ),
        Scenario(
            name="source_profile_detail",
            rows=50_000,
            budget_ms_p95=2000,
            seed=seed_source,
            run=run_source,
        ),
        Scenario(
            name="market_detail_with_signals",
            rows=500,
            budget_ms_p95=2000,
            seed=seed_market,
            run=run_market,
        ),
        Scenario(
            name="admin_users_list",
            rows=3_000,
            budget_ms_p95=1500,
            seed=seed_admin,
            run=run_admin,
        ),
        Scenario(
            name="takes_hot_market",
            rows=200,
            budget_ms_p95=1000,
            seed=seed_takes,
            run=run_takes,
        ),
    ]


# ── Driver ─────────────────────────────────────────────────────────────────


def _time_scenario(scenario: Scenario, iterations: int) -> dict:
    """Spin up a temp DB, seed, run `iterations` timed iterations of
    the scenario's hot query, return latency percentiles in ms."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        with sqlite3.connect(tmp.name) as conn:
            # Match prod's WAL settings so we measure realistic latency.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.row_factory = sqlite3.Row
            _create_schema(conn)
            scenario.seed(conn)
            # Warm cache with one run we don't count.
            scenario.run(conn)
            samples_ns: list[int] = []
            for _ in range(iterations):
                t0 = time.perf_counter_ns()
                scenario.run(conn)
                samples_ns.append(time.perf_counter_ns() - t0)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    samples_ms = sorted(s / 1_000_000 for s in samples_ns)
    p50 = samples_ms[len(samples_ms) // 2]
    p95 = samples_ms[min(int(len(samples_ms) * 0.95), len(samples_ms) - 1)]
    return {
        "p50_ms": p50,
        "p95_ms": p95,
        "min_ms": samples_ms[0],
        "max_ms": samples_ms[-1],
        "mean_ms": statistics.mean(samples_ms),
        "iterations": iterations,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--scenario", help="Run one scenario by name",
    )
    ap.add_argument(
        "--iterations", type=int, default=20,
        help="Samples per scenario (default 20)",
    )
    args = ap.parse_args()

    scenarios = _mk_scenarios()
    if args.scenario:
        scenarios = [s for s in scenarios if s.name == args.scenario]
        if not scenarios:
            print(f"unknown scenario: {args.scenario}", file=sys.stderr)
            return 2

    busted = 0
    header = f"  {'scenario':30s}{'rows':>10}{'p50(ms)':>10}{'p95(ms)':>10}{'budget':>10}  status"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for scn in scenarios:
        stats = _time_scenario(scn, args.iterations)
        ok = stats["p95_ms"] <= scn.budget_ms_p95
        status = "OK" if ok else "BUSTED"
        if not ok:
            busted += 1
        print(
            f"  {scn.name:30s}"
            f"{scn.rows:>10d}"
            f"{stats['p50_ms']:>10.1f}"
            f"{stats['p95_ms']:>10.1f}"
            f"{int(scn.budget_ms_p95):>10d}"
            f"  {status}"
        )

    if busted:
        print(f"\n❌ {busted} scenario(s) over budget", file=sys.stderr)
        return 1
    print("\n✓ all scenarios within budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
