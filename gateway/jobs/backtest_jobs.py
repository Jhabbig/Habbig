"""Async backtest runner.

Kicked off from POST /dashboard/backtest (backtest_routes.py). Pulls the
specific run row, hands it to ``backtest.run_backtest``, which does all
the DB work. We add a thin wrapper so the job registry sees a single
callable.
"""

from __future__ import annotations

import logging
from typing import Any

from jobs.registry import register_job


log = logging.getLogger("jobs.backtest")


@register_job("run_backtest")
async def run_backtest_job(run_id: int) -> dict[str, Any]:
    """Run one backtest synchronously inside the async worker.

    The engine is CPU-bound but the data sets fit in a few thousand
    rows, so it's fine to keep on the main event loop. If sizes grow,
    swap to asyncio.to_thread.
    """
    import backtest as _bt
    try:
        result = _bt.run_backtest(int(run_id))
        # Trim heavy arrays for the return value — the full result is
        # already persisted in backtest_runs.result_json.
        summary = {k: result.get(k) for k in (
            "starting_bankroll", "final_bankroll", "roi_pct", "win_rate",
            "bet_count", "wins", "losses", "sharpe", "max_drawdown",
        )}
        summary["run_id"] = run_id
        return summary
    except Exception as exc:
        log.exception("backtest run %s failed", run_id)
        return {"run_id": run_id, "error": str(exc)}
