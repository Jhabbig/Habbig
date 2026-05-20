"""Head-to-head: LLM recommendation vs raw model signal.

Both surfaces produce a buy/sell/pass call from the same forecast +
market state. The raw model signal is a function of the edge alone:

    edge >=  threshold → BUY_YES
    edge <= -threshold → BUY_NO
    otherwise          → PASS

The LLM call is `insight.recommendation`, which incorporates the same
edge plus intraday data, station skill, lead-time uncertainty, and any
reasoning the model brings to bear. The interesting question is whether
that extra context actually moves win rate or PnL — or whether the LLM
just adds latency and cost on top of the raw signal.

This module computes the joint statistics over resolved insights. It
pairs insight_log with insight_resolutions (already populated by
`resolve_insights`) and bins by `(raw_call, llm_call)` so the
disagreement cells are visible — when they differ, who's right more
often?
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def raw_signal_call(edge: Optional[float], threshold: float = 0.05) -> str:
    """Translate a numeric edge into the raw-signal three-way call."""
    if edge is None:
        return "PASS"
    try:
        e = float(edge)
    except (TypeError, ValueError):
        return "PASS"
    if e >= threshold:
        return "BUY_YES"
    if e <= -threshold:
        return "BUY_NO"
    return "PASS"


def _bet_pnl(call: str, yes_price: Optional[float], outcome: str) -> Optional[float]:
    """PnL per $1 staked if you'd taken `call` at `yes_price` and the
    market resolved to `outcome`.

    Raw-signal PnL uses the market price (no edge for the model to give
    itself a better entry). LLM PnL uses the suggested limit when
    present — see insight_storage._bet_pnl_per_dollar.
    """
    if call in ("PASS", "WAIT_AND_SEE", None):
        return 0.0
    if yes_price is None:
        return None
    if call == "BUY_YES":
        return round((1.0 - float(yes_price)) if outcome == "YES" else -float(yes_price), 4)
    if call == "BUY_NO":
        no_price = 1.0 - float(yes_price)
        return round((1.0 - no_price) if outcome == "NO" else -no_price, 4)
    return None


def _was_right(call: str, outcome: str) -> Optional[int]:
    """1 if the call matched the outcome, 0 if not, None for PASS / WAIT
    (no bet → can't be wrong)."""
    if call == "BUY_YES":
        return 1 if outcome == "YES" else 0
    if call == "BUY_NO":
        return 1 if outcome == "NO" else 0
    return None


def head_to_head_stats(conn_factory, days: int = 180,
                       raw_threshold: float = 0.05) -> dict:
    """Compute the head-to-head summary over resolved insights.

    Strategy: pull every resolved insight along with the stored
    `yes_price`, `edge`, and `recommendation` (the LLM's call). Derive
    the raw signal from `edge`. Score both against the actual outcome.
    Bucket by (raw_call, llm_call) so the agreement / disagreement
    cells are visible.

    Returns
    -------
    dict with keys:
        days, n
        raw     {wins, win_rate, total_pnl, avg_pnl, n_betted}
        llm     {wins, win_rate, total_pnl, avg_pnl, n_betted}
        agreement_rate
        when_disagree {raw_win_rate, llm_win_rate, n}
        matrix  {raw_call: {llm_call: {n, raw_wins, llm_wins,
                                       raw_pnl, llm_pnl}}}
    """
    days = max(1, min(365, int(days)))
    with conn_factory(readonly=True) as conn:
        rows = conn.execute(
            """SELECT i.recommendation, i.edge, i.yes_price,
                      i.suggested_limit_cents,
                      r.actual_outcome, r.pnl_per_dollar AS llm_pnl
               FROM insight_log i
               JOIN insight_resolutions r ON r.insight_id = i.id
               WHERE i.generated_at >= datetime('now', ?)
                 AND r.actual_outcome IN ('YES', 'NO')""",
            (f"-{days} days",),
        ).fetchall()

    matrix: dict = {}
    raw_wins = 0
    raw_pnl_total = 0.0
    raw_betted = 0
    llm_wins = 0
    llm_pnl_total = 0.0
    llm_betted = 0
    agreements = 0
    disagreements = 0
    disagree_raw_wins = 0
    disagree_llm_wins = 0
    disagree_betted = 0

    for r in rows:
        outcome = r["actual_outcome"]
        raw_call = raw_signal_call(r["edge"], threshold=raw_threshold)
        llm_call = r["recommendation"] or "PASS"

        raw_right = _was_right(raw_call, outcome)
        llm_right = _was_right(llm_call, outcome)
        raw_pnl = _bet_pnl(raw_call, r["yes_price"], outcome) or 0.0
        llm_pnl_val = float(r["llm_pnl"] or 0.0)

        # Per-bet totals (skip PASS/WAIT for the "betted" counters)
        if raw_right is not None:
            raw_betted += 1
            raw_wins += raw_right
            raw_pnl_total += raw_pnl
        if llm_right is not None:
            llm_betted += 1
            llm_wins += llm_right
            llm_pnl_total += llm_pnl_val

        # Agreement classification
        if raw_call == llm_call:
            agreements += 1
        else:
            disagreements += 1
            # Only score the disagreement cells where at least one
            # side actually placed a bet — comparing PASS-vs-PASS is
            # uninteresting and would be counted as agreement anyway.
            if raw_right is not None or llm_right is not None:
                disagree_betted += 1
                if raw_right == 1:
                    disagree_raw_wins += 1
                if llm_right == 1:
                    disagree_llm_wins += 1

        cell = matrix.setdefault(raw_call, {}).setdefault(
            llm_call, {"n": 0, "raw_wins": 0, "llm_wins": 0,
                       "raw_pnl": 0.0, "llm_pnl": 0.0}
        )
        cell["n"] += 1
        if raw_right == 1:
            cell["raw_wins"] += 1
        if llm_right == 1:
            cell["llm_wins"] += 1
        cell["raw_pnl"] = round(cell["raw_pnl"] + raw_pnl, 4)
        cell["llm_pnl"] = round(cell["llm_pnl"] + llm_pnl_val, 4)

    def _rate(num, denom):
        return round(num / denom, 4) if denom else None

    return {
        "days": days,
        "n": len(rows),
        "agreement_rate": _rate(agreements, len(rows)),
        "raw": {
            "n_betted": raw_betted,
            "wins": raw_wins,
            "win_rate": _rate(raw_wins, raw_betted),
            "total_pnl": round(raw_pnl_total, 4),
            "avg_pnl": (round(raw_pnl_total / raw_betted, 4)
                        if raw_betted else None),
            "threshold": raw_threshold,
        },
        "llm": {
            "n_betted": llm_betted,
            "wins": llm_wins,
            "win_rate": _rate(llm_wins, llm_betted),
            "total_pnl": round(llm_pnl_total, 4),
            "avg_pnl": (round(llm_pnl_total / llm_betted, 4)
                        if llm_betted else None),
        },
        "when_disagree": {
            "n": disagreements,
            "n_betted": disagree_betted,
            "raw_win_rate": _rate(disagree_raw_wins, disagree_betted),
            "llm_win_rate": _rate(disagree_llm_wins, disagree_betted),
        },
        "matrix": matrix,
    }
