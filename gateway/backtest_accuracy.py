"""Accuracy-first scoring for the walk-forward backtest.

The ROI/Sharpe numbers from backtest_replay are a BETTING metric — on the
synthetic fixture they are inflated by Kelly staking on a rigged answer and say
nothing about real skill. The honest "does the prediction engine work" question
is ACCURACY + CALIBRATION, scored on EVERY market (not only the ones narve bet
on). This module produces that.

For each market it records narve's point-in-time probability (from the same
walk-forward, no-lookahead logic the harness uses) and the market's own
probability at the same decision date, then scores both against the resolved
outcome:

  - directional accuracy: did P(yes) land on the correct side of 0.5?
  - Brier score (lower = better) + calibration (1 - brier/0.25, higher = better),
    reusing credibility.calibration.compute_brier_score.

It compares narve vs. the market head-to-head so the report answers "is narve
more accurate than just trusting the price?" — the metric that matters.

Reuses backtest_replay for the no-lookahead credibility/probability calc; adds
no new prediction logic of its own.
"""
from __future__ import annotations

from typing import Any, Optional

import backtest_replay as _replay

try:
    from credibility.calibration import compute_brier_score as _brier
except Exception:  # pragma: no cover - calibration module always present in repo
    _brier = None


def _accuracy(records: list[dict], key: str) -> dict:
    """Directional accuracy + Brier for a list of {p, outcome} records.

    `key` selects which probability to score ("narve" or "market").
    """
    scored = [r for r in records if r.get(key) is not None and r.get("resolved_outcome") is not None]
    n = len(scored)
    if n == 0:
        return {"n": 0, "correct": 0, "accuracy": None, "brier": None, "calibration": None}
    correct = 0
    for r in scored:
        p = float(r[key])
        o = int(r["resolved_outcome"])
        pred_yes = p >= 0.5
        if (pred_yes and o == 1) or (not pred_yes and o == 0):
            correct += 1
    # Brier via the shared helper (it needs >=10 records; fall back to manual).
    brier = calib = None
    recs = [{"predicted_probability_stated": float(r[key]), "resolved_correct": int(r["resolved_outcome"])} for r in scored]
    if _brier is not None:
        out = _brier(recs)
        if out:
            brier, calib = out["brier"], out["calibration"]
    if brier is None:  # manual fallback for small N (the demo fixture)
        total = sum((float(r[key]) - float(r["resolved_outcome"])) ** 2 for r in scored)
        brier = round(total / n, 6)
        calib = round(max(0.0, min(1.0, 1.0 - brier / 0.25)), 6)
    return {
        "n": n,
        "correct": correct,
        "accuracy": round(correct / n, 4),
        "brier": brier,
        "calibration": calib,
    }


def score_accuracy(dataset: list[dict], *, cold_start: str = "bayesian",
                   edge_threshold: float = 0.0) -> dict:
    """Score narve vs. the market on EVERY market in the dataset.

    Returns:
      {
        "cold_start": str,
        "records": [ per-market {market_id, question, resolved_at, resolved_outcome,
                                 narve, market, narve_correct, market_correct} ],
        "narve":  {n, correct, accuracy, brier, calibration},
        "market": {n, correct, accuracy, brier, calibration},
      }
    narve's probability per market is taken from the harness decision log where
    available (it logs narve_probability + market_price even when it chose not to
    bet, as long as a signal existed). Markets with no narve signal at any
    decision date are reported as "no signal" and excluded from narve's accuracy
    (we never credit narve for a guess it didn't make), but still counted for the
    market baseline.
    """
    # Run the walk-forward once; ask it to log a verdict on every market (bet or
    # not) by using a 0 edge threshold so every market with a signal is recorded.
    out = _replay.run_replay(dataset, cold_start=cold_start, edge_threshold=edge_threshold)
    by_market: dict[str, dict] = {}
    for d in out.get("decisions", []):
        mid = d.get("market_id")
        # keep the LAST (latest, most-informed) decision per market
        by_market[mid] = d

    records: list[dict] = []
    for m in dataset:
        mid = m.get("market_id")
        outcome = m.get("resolved_outcome")
        d = by_market.get(mid)
        narve_p = float(d["narve_probability"]) if d and d.get("narve_probability") is not None else None
        # market probability: last price at/just before resolution
        market_p = None
        if d and d.get("market_price") is not None:
            market_p = float(d["market_price"])
        else:
            tl = m.get("price_timeline") or []
            if tl:
                market_p = float(tl[-1].get("yes_price"))
        rec = {
            "market_id": mid,
            "question": m.get("question"),
            "resolved_at": m.get("resolved_at"),
            "resolved_outcome": outcome,
            "narve": narve_p,
            "market": market_p,
            "narve_correct": None if narve_p is None or outcome is None else int((narve_p >= 0.5) == (outcome == 1)),
            "market_correct": None if market_p is None or outcome is None else int((market_p >= 0.5) == (outcome == 1)),
            "had_signal": narve_p is not None,
        }
        records.append(rec)

    return {
        "cold_start": cold_start,
        "records": records,
        "narve": _accuracy([r for r in records if r["narve"] is not None], "narve"),
        "market": _accuracy(records, "market"),
    }


if __name__ == "__main__":  # quick manual run
    import json, sys
    import backtest_dataset as dl
    path = sys.argv[1] if len(sys.argv) > 1 else "data/backtest/synthetic_fixture.json"
    ds = dl.load_dataset(path)
    for method in ("strict_two_window", "bayesian"):
        r = score_accuracy(ds, cold_start=method)
        print(f"\n== {method} ==")
        print("  narve :", r["narve"])
        print("  market:", r["market"])
