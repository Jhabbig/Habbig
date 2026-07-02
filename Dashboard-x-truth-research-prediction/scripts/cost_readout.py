#!/usr/bin/env python3
"""One-page cost readout for the Prediction Engine.

Aggregates the fusion_audit table (measured tokens and dollars, not estimates)
into the deliverable summary: tokens/job, cache hit rate, $/1k predictions,
tier mix, degraded rate — plus a monthly projection at a target job volume.

    python scripts/cost_readout.py                # against the app DB
    python scripts/cost_readout.py --jobs-per-month 1000000
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `app` importable


import argparse
import asyncio


async def run(jobs_per_month: int) -> int:
    import app.db as db
    from app.engine.replay import cost_readout

    async with db.AsyncSession(db.engine, expire_on_commit=False) as session:
        readout = await cost_readout(session)

    print("=" * 60)
    print("PREDICTION ENGINE — COST READOUT")
    print("=" * 60)
    if not readout["jobs"]:
        print("No audited predictions yet. Run traffic (or the load test) first.")
        return 1
    print(f"predictions audited        : {readout['jobs']:,}")
    print(f"tokens in / out            : {readout['tokens_in']:,} / {readout['tokens_out']:,}")
    print(f"  of which cached input    : {readout['cached_tokens_in']:,}")
    print(f"avg tokens per job         : {readout['avg_tokens_per_job']}")
    print(f"cache hit rate             : {readout['cache_hit_rate']:.1%}")
    print(f"degraded rate              : {readout['degraded_rate']:.1%}")
    print(f"model-tier mix             : {readout['tier_mix']}")
    print(f"total measured cost        : ${readout['total_cost_usd']:.4f}")
    print(f"$ per 1k predictions       : ${readout['cost_per_1k_predictions_usd']:.4f}"
          f"  (alert threshold ${readout['cost_alert_threshold_usd_per_1k']})")
    if readout["cost_alert"]:
        print("⚠ COST ALERT: $/1k predictions exceeds the configured threshold")
    monthly = readout["cost_per_1k_predictions_usd"] / 1000 * jobs_per_month
    print(f"projected @ {jobs_per_month:,} jobs/mo : ${monthly:,.2f}/month")
    print("=" * 60)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-per-month", type=int, default=1_000_000)
    args = parser.parse_args()
    return asyncio.run(run(args.jobs_per_month))


if __name__ == "__main__":
    sys.exit(main())
