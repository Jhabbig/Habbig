"""Daily job — mark each take's `resolved_correct` once its market settles.

Scheduled daily at 03:11 UTC (off-peak, comfortably after Polymarket /
Kalshi settlement windows that typically land around midnight UTC).

Algorithm:

  1. Pick every `market_id` from `predictions` that got `resolved=1` in
     the last 48h AND has at least one take on it whose `resolved_correct`
     is still NULL (guard → avoids scoring a market whose takes are all
     already scored).
  2. For each market, derive an outcome ∈ {yes, no, None}:
       - if any resolved prediction has direction=YES and resolved_correct=1
         OR direction=NO and resolved_correct=0 → outcome = "yes"
       - if any resolved prediction has direction=YES and resolved_correct=0
         OR direction=NO and resolved_correct=1 → outcome = "no"
       - ambiguous (both present) → skip the market this run; it gets
         another chance next run.
  3. Call `db_takes.mark_takes_resolved_for_market(slug, outcome)`.
  4. Write a summary row to `take_resolution_runs`.

48h window (vs 24h) so a market that resolved just before the job last
ran still gets picked up on the next run. The unique partial index
`idx_takes_unresolved` means a scored take never gets re-scored — re-runs
are safe.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import db
import db_takes
from jobs.registry import register_cron, register_job, job_registry


log = logging.getLogger("jobs.take_resolution")


_JOB_NAME = "resolve_takes_for_finished_markets"


_LOOKBACK_SECONDS = 48 * 60 * 60


def _derive_outcome_for_market(market_id: str) -> tuple[str | None, bool]:
    """Look at every resolved prediction on this market and derive the
    true outcome. Returns (outcome, ambiguous).

    "ambiguous" means we saw evidence in both directions — refuse to
    score rather than guess wrong.
    """
    with db.conn() as c:
        rows = c.execute(
            "SELECT direction, resolved_correct FROM predictions "
            "WHERE market_id = ? AND resolved = 1 "
            "AND direction IS NOT NULL AND resolved_correct IS NOT NULL",
            (market_id,),
        ).fetchall()
    yes_votes = 0
    no_votes = 0
    for r in rows:
        d = (r["direction"] or "").strip().upper()
        rc = int(r["resolved_correct"])
        if d == "YES":
            if rc == 1:
                yes_votes += 1
            elif rc == 0:
                no_votes += 1
        elif d == "NO":
            if rc == 1:
                no_votes += 1
            elif rc == 0:
                yes_votes += 1
    if yes_votes and no_votes:
        return None, True
    if yes_votes:
        return "yes", False
    if no_votes:
        return "no", False
    return None, False


async def _resolve_takes_for_finished_markets_impl() -> dict[str, Any]:
    """Score each take whose market resolved in the last 48h."""
    run_id = db_takes.start_resolution_run()

    markets_considered = 0
    takes_resolved = 0
    takes_correct = 0
    takes_incorrect = 0
    error: str | None = None

    try:
        cutoff = int(time.time()) - _LOOKBACK_SECONDS
        # Find every market slug that (a) has a recent resolution AND (b)
        # has at least one unresolved take sitting on it.
        with db.conn() as c:
            rows = c.execute(
                "SELECT DISTINCT p.market_id FROM predictions p "
                "WHERE p.resolved = 1 AND p.resolved_at >= ? "
                "AND p.market_id IS NOT NULL "
                "AND EXISTS ("
                "  SELECT 1 FROM market_takes mt "
                "  WHERE mt.market_slug = p.market_id "
                "    AND mt.is_deleted = 0 "
                "    AND mt.resolved_correct IS NULL"
                ")",
                (cutoff,),
            ).fetchall()
        market_ids = [r["market_id"] for r in rows if r["market_id"]]
        markets_considered = len(market_ids)

        for slug in market_ids:
            outcome, ambiguous = _derive_outcome_for_market(slug)
            if ambiguous:
                log.warning(
                    "take-resolver: ambiguous resolution for %s — skipping this run",
                    slug,
                )
                continue
            if outcome is None:
                continue
            result = db_takes.mark_takes_resolved_for_market(slug, outcome)
            takes_resolved += int(result.get("scored", 0) or 0)
            takes_correct += int(result.get("correct", 0) or 0)
            takes_incorrect += int(result.get("incorrect", 0) or 0)

        log.info(
            "take-resolver: markets=%d takes_resolved=%d correct=%d incorrect=%d",
            markets_considered, takes_resolved, takes_correct, takes_incorrect,
        )

    except Exception as e:  # pragma: no cover — best-effort daily batch
        error = f"{type(e).__name__}: {e}"[:200]
        log.exception("take-resolver: run failed")
        raise
    finally:
        db_takes.finish_resolution_run(
            run_id,
            markets_considered=markets_considered,
            takes_resolved=takes_resolved,
            takes_correct=takes_correct,
            takes_incorrect=takes_incorrect,
            error=error,
        )

    return {
        "markets_considered": markets_considered,
        "takes_resolved": takes_resolved,
        "takes_correct": takes_correct,
        "takes_incorrect": takes_incorrect,
    }


# Register once per process. The pytest reload-safe pattern around
# jobs/__init__.py can re-execute this module body more than once; the
# guard makes that a no-op instead of a ValueError-on-duplicate.
if _JOB_NAME not in job_registry:
    resolve_takes_for_finished_markets = register_job(_JOB_NAME)(
        _resolve_takes_for_finished_markets_impl
    )
    # Daily at 03:11 UTC — avoid :00 / :30 clustering, well after the
    # nightly Polymarket/Kalshi settlement windows finish writing.
    register_cron(_JOB_NAME, minute=11, hour=3)
else:
    resolve_takes_for_finished_markets = job_registry[_JOB_NAME]
