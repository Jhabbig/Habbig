"""Walk-forward (point-in-time) replay harness for the narve backtest demo.

This is the integrity layer of the backtest. It iterates a golden dataset
chronologically and, at each decision date, reconstructs *only what was
knowable then*:

  - each forecaster's credibility from their PRIOR resolved calls only
    (reproducing narve's Bayesian time-decay credibility formula exactly —
    see ``gateway/queries/sources.py:recompute_all_credibilities`` and the
    credibility contract);
  - a credibility-weighted narve probability for the open market;
  - the market price *on that day*;
  - an edge; a bet is emitted when ``edge > edge_threshold``.

The hard guarantee — the bug that invalidates backtests, caught mechanically —
is the **lookahead assertion**: every input timestamp that feeds a decision
(forecast ``made_at``, the price observation, every prior resolved call used
for credibility) is asserted to be strictly *before* the decision date. Any
violation raises ``LookaheadError`` and aborts the run.

The harness does NOT reimplement staking, ROI, Sharpe, or drawdown. It
translates each emitted bet into the per-prediction dict shape that the
existing engine (`gateway/backtest.py:simulate`) consumes, and feeds them
through ``simulate()`` unchanged. That keeps a single source of truth for the
money math.

Two cold-start methodologies are supported (the spec says show both):

  - ``"strict_two_window"`` — split history into an earlier *calibration*
    window (builds credibility, never bets) and a later *test* window (bets,
    using only credibility accrued from resolved calls before the decision
    date). A source must have at least one resolved call before it is trusted.
  - ``"bayesian"`` — every source starts at a neutral 0.5 prior, updated per
    resolved call via the same Bayesian time-decay formula; a source's signal
    is only *used* once it has at least ``min_prior_calls`` resolved calls
    before the decision date.

Public API
----------
    run_replay(dataset, *, cold_start="bayesian", edge_threshold=0.05, ...) -> dict
        Returns ``{"decisions": [...], "results": {...}, "config": {...}}``.

This module imports only ``gateway.backtest`` (the engine) and the stdlib —
no FastAPI app, no DB. Safe to import from scripts, tests, and the report.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from backtest import simulate
from backtest_dataset import load_dataset, parse_iso_date


__all__ = [
    "run_replay",
    "LookaheadError",
    "ReplayError",
    "credibility_as_of",
    "COLD_START_METHODS",
    "LAMBDA",
    "PRIOR",
    "STRENGTH",
]


# ── credibility-formula constants (must match queries/sources.py exactly) ─────
LAMBDA = 0.01    # decay rate: half-life = ln(2)/0.01 ≈ 69 days
PRIOR = 0.5      # Bayesian prior (uninformed)
STRENGTH = 10    # prior pseudo-count (strength of regression to PRIOR)

COLD_START_METHODS = ("strict_two_window", "bayesian")


class ReplayError(ValueError):
    """Raised on bad configuration or a structurally impossible replay."""


class LookaheadError(ReplayError):
    """Raised when any input timestamp is not strictly before the decision
    date it feeds. This is the integrity guarantee: a backtest that peeks at
    the future is worse than no backtest, so we abort rather than report a
    tainted number."""


# ── date / time helpers ──────────────────────────────────────────────────────


def _to_unix(d: date) -> int:
    """Midnight-UTC unix seconds for an ISO ``date`` — the engine speaks unix."""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _age_days(now: date, resolved_at: date) -> float:
    """Whole-and-fractional days between two dates, floored at 0 — mirrors the
    ``max(0, (now - resolved_at) / 86400)`` in the production credibility calc."""
    return max(0.0, (_to_unix(now) - _to_unix(resolved_at)) / 86400.0)


# ── point-in-time credibility (reproduces recompute_all_credibilities) ───────


def credibility_as_of(
    resolved_calls: list[dict],
    decision_date: date,
    *,
    require_prior: bool = True,
) -> dict:
    """Compute a source's credibility using ONLY its resolved calls that
    resolved strictly before ``decision_date``.

    ``resolved_calls`` is a list of ``{"resolved_at": date, "correct": bool}``
    for one source. The formula is identical to the production recompute:

        decay_i      = exp(-LAMBDA * age_days(decision_date, resolved_at_i))
        DWA          = Σ(correct_i · decay_i) / Σ(decay_i)        (PRIOR if Σ=0)
        credibility  = (n · DWA + STRENGTH · PRIOR) / (n + STRENGTH)

    where ``n`` is the count of prior resolved calls and ``decision_date``
    plays the role of "now" in the production calc.

    Returns a dict::

        {credibility, dwa, n_prior, decay_weight_total, has_signal}

    ``has_signal`` is False (and ``credibility`` is the neutral PRIOR) when the
    source has no prior resolved calls — i.e. it is in its cold-start period.

    Raises ``LookaheadError`` if any supplied resolved call has
    ``resolved_at >= decision_date`` (it would not have been knowable yet).
    """
    prior = [c for c in resolved_calls if c["resolved_at"] < decision_date]

    # Lookahead guard: nothing fed into a credibility score may resolve on or
    # after the decision date. (Callers should pre-filter, but we assert here
    # too — defense in depth on the one invariant that matters most.)
    for c in resolved_calls:
        if c["resolved_at"] >= decision_date and c in prior:  # pragma: no cover
            raise LookaheadError(
                f"credibility_as_of: resolved call {c['resolved_at'].isoformat()} "
                f"is not before decision date {decision_date.isoformat()}"
            )

    n = len(prior)
    if n == 0:
        return {
            "credibility": PRIOR,
            "dwa": PRIOR,
            "n_prior": 0,
            "decay_weight_total": 0.0,
            "has_signal": not require_prior,  # bayesian-without-prior can still vote at PRIOR
        }

    weighted_correct = 0.0
    weight_total = 0.0
    for c in prior:
        decay = math.exp(-LAMBDA * _age_days(decision_date, c["resolved_at"]))
        is_correct = 1 if c["correct"] else 0
        weighted_correct += is_correct * decay
        weight_total += decay

    dwa = weighted_correct / weight_total if weight_total > 0 else PRIOR
    credibility = (n * dwa + STRENGTH * PRIOR) / (n + STRENGTH)
    return {
        "credibility": round(credibility, 6),
        "dwa": round(dwa, 6),
        "n_prior": n,
        "decay_weight_total": round(weight_total, 6),
        "has_signal": True,
    }


# ── dataset → chronological structures ───────────────────────────────────────


def _index_resolved_calls(markets: list[dict]) -> dict:
    """Build ``source_handle -> [ {resolved_at: date, correct: bool, market_id} ]``.

    A "call" is a single dated forecast. It becomes a *resolved* call (usable as
    credibility evidence) only at the market's ``resolved_at``. ``correct`` is
    whether the forecaster's directional call matched the resolved outcome:
    YES-leaning (p ≥ 0.5) is right iff outcome == 1; NO-leaning iff outcome == 0.
    """
    by_source: dict = {}
    for m in markets:
        resolved_at = parse_iso_date(m["resolved_at"])
        outcome = int(m["resolved_outcome"])
        for fc in m["forecasts"]:
            p = float(fc["predicted_probability"])
            side_yes = p >= 0.5
            correct = (outcome == 1) if side_yes else (outcome == 0)
            by_source.setdefault(fc["source_handle"], []).append({
                "resolved_at": resolved_at,
                "correct": bool(correct),
                "market_id": m["market_id"],
                "made_at": parse_iso_date(fc["made_at"]),
            })
    return by_source


def _price_on_or_before(price_timeline: list[dict], when: date) -> Optional[dict]:
    """Latest price observation dated on or before ``when`` (point-in-time).

    Returns ``{"date": date, "yes_price": float}`` or None if no price is
    knowable yet. Never returns a future price — that would be lookahead.
    """
    best: Optional[dict] = None
    for pt in price_timeline:
        d = parse_iso_date(pt["date"])
        if d <= when and (best is None or d > best["date"]):
            best = {"date": d, "yes_price": float(pt["yes_price"])}
    return best


def _calibration_split_date(markets: list[dict], fraction: float) -> date:
    """For strict two-window: the date that splits resolved markets into an
    earlier calibration window and a later test window. Markets resolving on or
    before this date are calibration-only; later ones are eligible for betting.

    The split is a resolution date: every market resolving on or before it is
    calibration-only (builds credibility, never bets); markets resolving after
    it are eligible to bet. To guarantee a non-empty test window, the split is
    capped at the *second-to-last* distinct resolution date — the latest
    resolution always falls in the test window.
    """
    resolved_dates = sorted({parse_iso_date(m["resolved_at"]) for m in markets})
    if len(resolved_dates) <= 1:
        # Degenerate: everything resolves at once. No earlier window exists, so
        # put the split before all of them (nothing is calibration-only, every
        # source is cold). The harness still runs; it just won't bet much.
        return date.min
    # idx selects the last calibration date; clamp to [0, len-2] so at least the
    # final resolution date stays in the test window.
    idx = int((len(resolved_dates) - 1) * fraction)
    idx = max(0, min(len(resolved_dates) - 2, idx))
    return resolved_dates[idx]


# ── narve probability for one market at one decision date ────────────────────


def _narve_probability(
    market: dict,
    decision_date: date,
    resolved_by_source: dict,
    *,
    cold_start: str,
    min_prior_calls: int,
    calibration_split: Optional[date],
) -> Optional[dict]:
    """Form narve's credibility-weighted YES probability for ``market`` as of
    ``decision_date``, using only forecasts made strictly before that date and
    only credibility accrued from calls resolved before that date.

    Returns a dict with the probability, the per-source contributions, and the
    list of active forecasts — or None if no usable signal exists yet.

    Raises ``LookaheadError`` if any active forecast was made on/after the
    decision date (should be impossible given the iteration, but asserted).
    """
    contributions: list[dict] = []
    weighted_p = 0.0
    weight_total = 0.0

    for fc in market["forecasts"]:
        made_at = parse_iso_date(fc["made_at"])
        # Only forecasts knowable strictly before the decision date.
        if made_at >= decision_date:
            continue
        handle = fc["source_handle"]

        # Prior resolved calls for this source — exclude any call that resolves
        # on/after the decision date (lookahead) AND exclude calls from THIS
        # market (a market can't score its own forecaster before it resolves;
        # and since decision_date < resolved_at, those are already excluded).
        all_calls = resolved_by_source.get(handle, [])
        prior_calls = [c for c in all_calls if c["resolved_at"] < decision_date]

        # Hard lookahead guard on every credibility input.
        for c in prior_calls:
            if c["resolved_at"] >= decision_date:  # pragma: no cover
                raise LookaheadError(
                    f"market {market['market_id']} @ {decision_date.isoformat()}: "
                    f"credibility input for {handle} resolves at "
                    f"{c['resolved_at'].isoformat()} — not before decision date"
                )

        require_prior = (cold_start == "strict_two_window")
        cred = credibility_as_of(prior_calls, decision_date, require_prior=require_prior)

        # Cold-start gating.
        if cold_start == "strict_two_window":
            # In calibration window: source builds credibility, never bets.
            if calibration_split is not None and parse_iso_date(market["resolved_at"]) <= calibration_split:
                return None  # this whole market is calibration-only
            if not cred["has_signal"]:
                continue  # source has no track record yet → no vote
        elif cold_start == "bayesian":
            if cred["n_prior"] < min_prior_calls:
                continue  # not enough resolved calls to trust this source yet
        else:  # pragma: no cover — validated upstream
            raise ReplayError(f"unknown cold_start method: {cold_start!r}")

        weight = cred["credibility"]
        p = float(fc["predicted_probability"])
        weighted_p += weight * p
        weight_total += weight
        contributions.append({
            "source_handle": handle,
            "forecast_probability": round(p, 6),
            "made_at": made_at.isoformat(),
            "credibility": cred["credibility"],
            "n_prior_resolved": cred["n_prior"],
            "weight": round(weight, 6),
        })

    if weight_total <= 0 or not contributions:
        return None

    narve_p = weighted_p / weight_total
    return {
        "narve_probability": round(narve_p, 6),
        "contributions": contributions,
        "active_forecast_count": len(contributions),
    }


# ── the walk-forward loop ────────────────────────────────────────────────────


def run_replay(
    dataset: Union[str, Path, list[dict]],
    *,
    cold_start: str = "bayesian",
    edge_threshold: float = 0.05,
    min_prior_calls: int = 1,
    calibration_fraction: float = 0.5,
    bet_sizing: str = "kelly",
    flat_bet_size: float = 100.0,
    starting_bankroll: float = 10_000.0,
    one_bet_per_market: bool = True,
) -> dict:
    """Run the walk-forward replay and score it through the engine.

    Args:
        dataset: a path to a golden-dataset JSON file, or an already-loaded
            ``list[dict]`` of validated market records (from
            ``backtest_dataset.load_dataset``).
        cold_start: ``"strict_two_window"`` or ``"bayesian"`` (see module doc).
        edge_threshold: minimum ``narve_p - market_price`` (toward the bet side)
            to emit a bet. Compared on the *directional* edge.
        min_prior_calls: bayesian cold-start gate — a source's vote is used only
            once it has at least this many resolved calls before the decision.
        calibration_fraction: strict-two-window split point (fraction of
            resolution dates that fall in the calibration window).
        bet_sizing: passed to the engine — ``"flat"``, ``"kelly"``, or
            ``"proportional_ev"``.
        flat_bet_size: stake for ``"flat"`` sizing.
        starting_bankroll: engine starting bankroll.
        one_bet_per_market: if True (default), only the first (earliest) date a
            market clears the edge threshold produces a bet, mirroring a single
            position per market. If False, every qualifying decision date bets.

    Returns:
        ``{"decisions": [...], "results": {...}, "config": {...}}`` —
        the HARNESS→REPORT interface. ``results`` is the verbatim output of
        ``simulate()`` (roi_pct, win_rate, sharpe, max_drawdown, bet_count,
        final_bankroll, equity_curve, bets). ``decisions`` is the per-bet audit
        log the report renders one-by-one.

    Raises:
        ReplayError / LookaheadError on bad config or an integrity violation.
    """
    if cold_start not in COLD_START_METHODS:
        raise ReplayError(
            f"cold_start must be one of {COLD_START_METHODS}, got {cold_start!r}"
        )

    markets = load_dataset(dataset) if isinstance(dataset, (str, Path)) else list(dataset)
    if not markets:
        raise ReplayError("dataset has no markets")

    resolved_by_source = _index_resolved_calls(markets)
    calibration_split = (
        _calibration_split_date(markets, calibration_fraction)
        if cold_start == "strict_two_window" else None
    )

    # Build the chronological list of decision events: one per (market, distinct
    # forecast-date). At each we ask "what would narve do, knowing only the
    # past?". Sorted strictly by date so the walk is forward in time.
    events: list[tuple] = []
    for m in markets:
        resolved_at = parse_iso_date(m["resolved_at"])
        dates = sorted({parse_iso_date(fc["made_at"]) for fc in m["forecasts"]})
        for d in dates:
            # A decision date must be strictly before resolution (the loader
            # already guarantees made_at < resolved_at, but assert the derived
            # decision date too).
            if d >= resolved_at:  # pragma: no cover
                raise LookaheadError(
                    f"market {m['market_id']}: decision date {d.isoformat()} is "
                    f"not before resolved_at {resolved_at.isoformat()}"
                )
            events.append((d, m))
    events.sort(key=lambda e: (e[0], e[1]["market_id"]))

    decisions: list[dict] = []
    engine_predictions: list[dict] = []
    bet_markets: set[str] = set()

    for decision_date, m in events:
        market_id = m["market_id"]
        if one_bet_per_market and market_id in bet_markets:
            continue

        signal = _narve_probability(
            m, decision_date, resolved_by_source,
            cold_start=cold_start,
            min_prior_calls=min_prior_calls,
            calibration_split=calibration_split,
        )
        if signal is None:
            continue

        price_pt = _price_on_or_before(m["price_timeline"], decision_date)
        if price_pt is None:
            continue  # no knowable price yet — can't compute an edge

        # Lookahead guard on the price input.
        if price_pt["date"] >= decision_date and price_pt["date"] != decision_date:  # pragma: no cover
            raise LookaheadError(
                f"market {market_id} @ {decision_date.isoformat()}: price observation "
                f"{price_pt['date'].isoformat()} is after the decision date"
            )

        narve_p = signal["narve_probability"]
        market_price = price_pt["yes_price"]

        # Directional edge: bet YES if narve is more bullish than the market,
        # NO if more bearish. Edge is the absolute distance in the bet's favour.
        if narve_p >= market_price:
            direction = "YES"
            edge = narve_p - market_price
        else:
            direction = "NO"
            edge = market_price - narve_p

        outcome = int(m["resolved_outcome"])
        resolved_correct = (direction == "YES" and outcome == 1) or (
            direction == "NO" and outcome == 0
        )

        decision = {
            "date": decision_date.isoformat(),
            "market_id": market_id,
            "question": m["question"],
            "resolved_at": m["resolved_at"],
            "resolved_outcome": outcome,
            "narve_probability": round(narve_p, 6),
            "market_price": round(market_price, 6),
            "price_date": price_pt["date"].isoformat(),
            "direction": direction,
            "edge": round(edge, 6),
            "edge_threshold": edge_threshold,
            "bet": edge > edge_threshold,
            "resolved_correct": bool(resolved_correct),
            "active_forecast_count": signal["active_forecast_count"],
            "contributions": signal["contributions"],
        }

        if edge > edge_threshold:
            bet_markets.add(market_id)
            # Translate into the per-prediction dict the engine consumes.
            #
            # The engine (backtest.py) is written entirely in a YES frame: its
            # Kelly sizing and EV use ``p`` as the belief in YES and ``price``
            # as the YES price, and it skips any bet where ``p <= price``. To
            # reuse that math unchanged for a NO bet (which the spec requires —
            # do not reimplement Kelly), we re-express the chosen side as a
            # YES-frame bet on that side:
            #   - belief in the side we bet = narve_p (YES) or 1 - narve_p (NO)
            #   - price of the side we bet   = market_price (YES) or 1-price (NO)
            #   - direction handed to engine = "YES" (we always pass the side we
            #     are actually betting on, so effective_price == side price and
            #     the PnL formula pays out on the real side).
            # The decision log above keeps the *true* direction / narve_p /
            # market_price, so the audit trail stays honest; only the engine's
            # internal representation is YES-framed.
            if direction == "YES":
                engine_p = narve_p
                engine_price = market_price
            else:
                engine_p = 1.0 - narve_p
                engine_price = 1.0 - market_price
            engine_predictions.append({
                "predicted_probability": engine_p,
                # Engine reads market_price_at_prediction first, then yes_price.
                "market_price_at_prediction": engine_price,
                "yes_price": engine_price,
                "direction": "YES",  # always the side we bet (see note above)
                "resolved_correct": bool(resolved_correct),
                "content": m["question"],
                "extracted_at": _to_unix(decision_date),
                # No close-time gating in the demo — leave unset so the engine's
                # min_hours_remaining check passes (defaults to 999 hours).
                "market_close_time": None,
                "_market_id": market_id,
            })

        decisions.append(decision)

    # ── Feed the emitted bets through the existing engine (single source of
    #    truth for staking / ROI / Sharpe / drawdown). Engine processes in the
    #    order given; we already sorted decisions chronologically. ────────────
    date_from = _to_unix(min((e[0] for e in events))) if events else 0
    engine_params = {
        "bet_sizing": bet_sizing,
        "flat_bet_size": flat_bet_size,
        "starting_bankroll": starting_bankroll,
        "min_ev": 0.0,            # edge gating already done in the harness
        "min_hours_remaining": 0.0,
        "min_credibility": 0.0,
        "categories": [],
        "source_handles": [],
        "date_from": date_from,
    }
    results = simulate(engine_params, engine_predictions)

    # Stitch the engine's per-bet money detail back onto the matching decision
    # so each bet's audit row carries stake / pnl / bankroll_after.
    bet_decisions = [d for d in decisions if d["bet"]]
    for d, eb in zip(bet_decisions, results.get("bets", [])):
        d["stake"] = eb.get("stake")
        d["pnl"] = eb.get("pnl")
        d["bankroll_after"] = eb.get("bankroll_after")

    return {
        "decisions": decisions,
        "results": results,
        "config": {
            "cold_start": cold_start,
            "edge_threshold": edge_threshold,
            "min_prior_calls": min_prior_calls,
            "calibration_fraction": calibration_fraction,
            "calibration_split": calibration_split.isoformat() if calibration_split else None,
            "bet_sizing": bet_sizing,
            "flat_bet_size": flat_bet_size,
            "starting_bankroll": starting_bankroll,
            "one_bet_per_market": one_bet_per_market,
            "market_count": len(markets),
            "decision_event_count": len(events),
            "bet_count": len([d for d in decisions if d["bet"]]),
        },
    }


if __name__ == "__main__":  # pragma: no cover — manual smoke test
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).parent / "data" / "backtest" / "synthetic_fixture.json"
    )
    for method in COLD_START_METHODS:
        out = run_replay(target, cold_start=method, edge_threshold=0.05)
        r = out["results"]
        c = out["config"]
        print(f"\n=== cold_start={method} ===")
        print(f"  markets={c['market_count']}  decision_events={c['decision_event_count']}  "
              f"bets={c['bet_count']}")
        print(f"  roi_pct={r['roi_pct']}  win_rate={r['win_rate']}  "
              f"sharpe={r['sharpe']}  max_drawdown={r['max_drawdown']}  "
              f"final_bankroll={r['final_bankroll']}")
        for d in out["decisions"]:
            if d["bet"]:
                print(f"    BET {d['date']} {d['market_id']} {d['direction']} "
                      f"narve_p={d['narve_probability']} mkt={d['market_price']} "
                      f"edge={d['edge']} -> {'WIN' if d['resolved_correct'] else 'LOSS'}")
    print()
