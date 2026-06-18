"""Scoring + report renderer for the walk-forward backtest demo.

This module is the **report layer** of the walk-forward backtest pipeline
(spec: ``docs/superpowers/specs/2026-06-18-walk-forward-backtest-demo-design.md``
§"3. Scoring + report"). It consumes the replay harness output and renders a
plain, auditable proof document — markdown *and* a self-contained HTML file —
for the YC pitch deck.

It deliberately reuses the existing engine helpers in ``gateway/backtest.py``
(``simulate``, ``sharpe_ratio``, ``max_drawdown``) so the baselines are scored
by the *same* code path as narve — a baseline computed by a different formula
would be a meaningless comparison.

--------------------------------------------------------------------------------
Consumed shape (the HARNESS -> REPORT interface — built to this exactly)
--------------------------------------------------------------------------------

The harness (``gateway/backtest_replay.py``) exposes::

    run_replay(dataset, cold_start, edge_threshold, ...) -> ReplayOutput

where ``ReplayOutput`` is a dict::

    {
      "decisions": [ Decision, ... ],   # per-bet decision log, chronological
      "results":   Results,             # headline metrics
    }

Each ``Decision`` (one logged bet narve placed) has at least::

    {
      "date":             str,    # ISO date the bet was decided (as-of date)
      "market_id":        str,    # which market
      "question":         str,    # optional, human-readable market question
      "credibility_contributions": {   # per-source as-of credibility inputs
          "@handle": {                 # the credibility KNOWN on `date` only
              "credibility":           float,   # as-of credibility weight
              "predicted_probability": float,   # that source's stated prob
          },
          ...
      },
      "narve_probability": float,   # narve's credibility-weighted P(YES)
      "market_price":      float,   # market YES price on `date`
      "edge":              float,   # narve_probability - market_price (signed)
      "direction":         str,     # "YES" / "NO" — side narve took (optional)
      "stake":             float,   # amount staked
      "resolved_correct":  bool,    # did the bet win at resolution
      "pnl":               float,   # optional realized P&L for the bet
    }

``Results``::

    {
      "roi_pct":        float,
      "win_rate":       float,   # 0..1
      "sharpe":         float,
      "max_drawdown":   float,   # 0..1 fraction
      "bet_count":      int,
      "final_bankroll": float,
      "starting_bankroll": float,   # optional; defaults to 10_000 for display
    }

--------------------------------------------------------------------------------
Public API
--------------------------------------------------------------------------------

    build_report(replay_outputs_by_method, *, out_dir=None, dataset_label=None)
        -> dict   # {"markdown": <path>, "html": <path>}

``replay_outputs_by_method`` maps a cold-start methodology label to its
``ReplayOutput``::

    {
      "Strict two-window": <ReplayOutput>,
      "Bayesian prior":    <ReplayOutput>,
    }

For each methodology the report computes the two mandatory baselines from that
methodology's own decision log (so they bet on the *same* markets on the *same
dates*) and renders narve-vs-baselines side by side, plus the full per-bet
audit table.
"""

from __future__ import annotations

import html
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

# Reuse the engine's scoring helpers so baselines are scored by the SAME code
# path as narve. Falls back to local copies if the engine cannot be imported
# (e.g. running this file in isolation), so the report module never hard-depends
# on the app being importable.
try:  # pragma: no cover - exercised implicitly by the test stub
    from gateway.backtest import max_drawdown as _engine_max_drawdown
    from gateway.backtest import sharpe_ratio as _engine_sharpe_ratio
except Exception:  # pragma: no cover
    try:
        from backtest import max_drawdown as _engine_max_drawdown  # type: ignore
        from backtest import sharpe_ratio as _engine_sharpe_ratio  # type: ignore
    except Exception:
        _engine_max_drawdown = None  # type: ignore
        _engine_sharpe_ratio = None  # type: ignore


__all__ = ["build_report", "score_baselines", "DEFAULT_OUT_DIR"]


DEFAULT_OUT_DIR = Path(__file__).parent / "data" / "backtest"
_DEFAULT_BANKROLL = 10_000.0


# ── local fallbacks (identical math to gateway/backtest.py) ───────────────────


def _sharpe_ratio(returns: list[float], *, risk_free: float = 0.0) -> float:
    if _engine_sharpe_ratio is not None:
        return _engine_sharpe_ratio(returns, risk_free=risk_free)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std <= 0:
        return 0.0
    return round((mean - risk_free) / std, 4)


def _max_drawdown(equity: list[float]) -> float:
    if _engine_max_drawdown is not None:
        return _engine_max_drawdown(equity)
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > worst:
                worst = dd
    return round(worst, 6)


# ── baseline scoring ──────────────────────────────────────────────────────────


def _decision_outcome(d: dict) -> int:
    """Recover the raw market outcome (1 = YES resolved, 0 = NO resolved).

    The decision log's ``resolved_correct`` is *narve-side-relative* — it says
    whether the side narve took won, not how the market resolved. A baseline may
    take a different side, so we need the raw outcome.

    Prefers an explicit ``resolved_outcome`` if the harness logs one; otherwise
    inverts narve's ``direction`` + ``resolved_correct``:

        YES bet + correct  -> YES (1)      YES bet + wrong -> NO  (0)
        NO  bet + correct  -> NO  (0)      NO  bet + wrong -> YES (1)
    """
    if "resolved_outcome" in d and d["resolved_outcome"] is not None:
        return 1 if int(d["resolved_outcome"]) == 1 else 0
    direction = str(d.get("direction") or "YES").upper()
    correct = bool(d.get("resolved_correct"))
    if direction == "YES":
        return 1 if correct else 0
    return 0 if correct else 1


def _bet_outcome_pnl(stake: float, effective_price: float, correct: bool) -> float:
    """P&L of a single resolved bet — mirrors gateway/backtest.py:210-215.

    ``effective_price`` is the YES price for a YES bet, or ``1 - price`` for a
    NO bet, clamped to [0.01, 0.99]. A correct bet pays out the implied odds;
    an incorrect bet loses the stake.
    """
    ep = max(0.01, min(0.99, float(effective_price)))
    if correct:
        return stake * (1.0 - ep) / ep
    return -stake


def _replay_from_rows(rows: list[dict], starting_bankroll: float) -> dict:
    """Score an ordered list of {price, p_belief, direction, outcome, ...} rows
    through the same sequential-bankroll mechanics the engine uses.

    Used for the two baselines: each baseline reinterprets a decision's belief
    (``p_belief``) and/or side, then we re-run the bankroll forward over the
    SAME markets in the SAME chronological order narve bet them.

    Each row carries the raw market ``outcome`` (1 = YES resolved, 0 = NO
    resolved). A bet is *correct* iff its own ``direction`` matches the outcome
    — this is what lets a baseline that picks a DIFFERENT side than narve be
    scored honestly (the decision log's ``resolved_correct`` is narve-side-
    relative and cannot be reused for a different side).

    A row with ``stake <= 0`` is a no-op (kept for alignment but not bet).
    """
    bankroll = float(starting_bankroll)
    equity = [bankroll]
    returns: list[float] = []
    bets: list[dict] = []
    wins = 0
    losses = 0

    for row in rows:
        stake = float(row.get("stake") or 0.0)
        if stake <= 0 or bankroll <= 0:
            continue
        stake = min(stake, bankroll)  # cannot stake more than we hold
        price = float(row["price"])
        direction = str(row.get("direction") or "YES").upper()
        effective_price = price if direction == "YES" else (1.0 - price)
        outcome = int(row["outcome"])  # 1 = YES resolved, 0 = NO resolved
        # Correct iff the side we took matches how the market actually resolved.
        correct = (outcome == 1) if direction == "YES" else (outcome == 0)
        pnl = _bet_outcome_pnl(stake, effective_price, correct)
        bankroll += pnl
        returns.append(pnl / max(stake, 1.0))
        if correct:
            wins += 1
        else:
            losses += 1
        bets.append({
            "date": row.get("date"),
            "market_id": row.get("market_id"),
            "question": row.get("question", ""),
            "p_belief": round(float(row.get("p_belief", price)), 4),
            "price": round(price, 4),
            "edge": round(float(row.get("p_belief", price)) - price, 4),
            "direction": direction,
            "stake": round(stake, 2),
            "outcome": "win" if correct else "loss",
            "pnl": round(pnl, 2),
            "bankroll_after": round(bankroll, 2),
        })
        equity.append(bankroll)

    total = wins + losses
    return {
        "starting_bankroll": round(starting_bankroll, 2),
        "final_bankroll": round(bankroll, 2),
        "roi_pct": round((bankroll - starting_bankroll) / starting_bankroll * 100.0, 4)
        if starting_bankroll else 0.0,
        "win_rate": round(wins / total, 4) if total else 0.0,
        "bet_count": total,
        "wins": wins,
        "losses": losses,
        "sharpe": _sharpe_ratio(returns),
        "max_drawdown": _max_drawdown(equity),
        "bets": bets,
    }


def score_baselines(decisions: list[dict], *, starting_bankroll: float = _DEFAULT_BANKROLL) -> dict:
    """Compute the two mandatory baselines from narve's decision log.

    Both baselines bet on the *exact same markets, on the same dates, with the
    same stakes* narve used — only the BELIEF / side differs. This isolates the
    value of narve's credibility weighting from the bet-selection and sizing.

    Returns ``{"follow_market": <results>, "flat_ensemble": <results>}`` where
    each value has the same metric keys as a harness ``results`` block plus a
    ``bets`` list. Returns empty (zeroed) results if there are no decisions.

    Baselines:

    * **always-follow-market** — believe exactly the market price. With no edge
      over the market, the rational stake is zero, so this baseline never bets
      (its number is a flat 0% ROI). We surface that explicitly: "trusting the
      market gives you the market" — the null hypothesis narve must beat.

    * **flat / unweighted ensemble** — believe the *equal-weight average* of all
      forecasters' stated probabilities for that market on that date (ignoring
      credibility entirely). Bets the same stake narve did, on the side that
      average implies vs. the market price.
    """
    follow_rows: list[dict] = []
    flat_rows: list[dict] = []

    for d in decisions:
        price = float(d["market_price"])
        stake = float(d.get("stake") or 0.0)
        outcome = _decision_outcome(d)
        common = {
            "date": d.get("date"),
            "market_id": d.get("market_id"),
            "question": d.get("question", ""),
            "price": price,
        }

        # --- always-follow-market: belief == price -> no edge -> no bet ---
        follow_rows.append({**common, "p_belief": price, "direction": "YES",
                            "stake": 0.0, "outcome": outcome})

        # --- flat ensemble: equal-weight average of stated forecaster probs ---
        # The harness emits `contributions` as a LIST of per-source dicts with a
        # `forecast_probability` field (not a dict keyed by handle, not
        # `predicted_probability`). Read that real shape.
        contribs = d.get("contributions") or []
        probs = [
            float(c["forecast_probability"])
            for c in contribs
            if isinstance(c, dict) and c.get("forecast_probability") is not None
        ]
        if probs:
            flat_p = sum(probs) / len(probs)
        else:
            flat_p = float(d.get("narve_probability", price))
        # Side the flat ensemble implies vs market; if it agrees with the
        # market (no edge) it still mirrors narve's stake to keep the
        # comparison on equal footing on the same selected markets. Scored on
        # ITS OWN side against the raw market outcome — a wrong-side flat bet
        # correctly registers as a loss.
        flat_dir = "YES" if flat_p >= price else "NO"
        flat_rows.append({**common, "p_belief": round(flat_p, 4),
                          "direction": flat_dir, "stake": stake, "outcome": outcome})

    return {
        "follow_market": _replay_from_rows(follow_rows, starting_bankroll),
        "flat_ensemble": _replay_from_rows(flat_rows, starting_bankroll),
    }


# ── narve results normalization ───────────────────────────────────────────────


def _narve_bets_from_decisions(decisions: list[dict], starting_bankroll: float) -> list[dict]:
    """Build a per-bet audit list for narve from the decision log.

    Prefers the harness's own ``pnl`` / running bankroll if present; otherwise
    reconstructs it with the same engine mechanics so the audit table always has
    a P&L and running bankroll column.
    """
    bankroll = float(starting_bankroll)
    out: list[dict] = []
    have_pnl = all("pnl" in d for d in decisions) and bool(decisions)

    for d in decisions:
        price = float(d["market_price"])
        stake = float(d.get("stake") or 0.0)
        direction = str(d.get("direction") or "YES").upper()
        correct = bool(d.get("resolved_correct"))
        if have_pnl:
            pnl = float(d["pnl"])
        else:
            effective_price = price if direction == "YES" else (1.0 - price)
            pnl = _bet_outcome_pnl(stake, effective_price, correct) if stake > 0 else 0.0
        bankroll += pnl
        out.append({
            "date": d.get("date", ""),
            "market_id": d.get("market_id", ""),
            "question": d.get("question", ""),
            "narve_probability": float(d.get("narve_probability", price)),
            "market_price": price,
            "edge": float(d.get("edge", float(d.get("narve_probability", price)) - price)),
            "direction": direction,
            "stake": round(stake, 2),
            "outcome": "win" if correct else "loss",
            "pnl": round(pnl, 2),
            "bankroll_after": round(bankroll, 2),
            "contributions": d.get("contributions") or [],
        })
    return out


def _starting_bankroll_of(output: dict) -> float:
    results = output.get("results") or {}
    sb = results.get("starting_bankroll")
    return float(sb) if sb else _DEFAULT_BANKROLL


# ── formatting helpers ─────────────────────────────────────────────────────────


def _pct(x: Optional[float], digits: int = 1) -> str:
    if x is None:
        return "—"
    return f"{float(x) * 100:.{digits}f}%"


def _signed_pct(x: Optional[float], digits: int = 1) -> str:
    if x is None:
        return "—"
    v = float(x) * 100
    return f"{v:+.{digits}f}%"


def _roi(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{float(x):+.{digits}f}%"


def _money(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"${float(x):,.0f}"


def _num(x: Optional[float], digits: int = 4) -> str:
    if x is None:
        return "—"
    return f"{float(x):.{digits}f}"


# ── markdown rendering ─────────────────────────────────────────────────────────


def _md_metrics_table(narve: dict, baselines: dict) -> str:
    fm = baselines["follow_market"]
    fe = baselines["flat_ensemble"]
    rows = [
        ("Metric", "narve (credibility-weighted)", "Follow market", "Flat ensemble"),
        ("ROI", _roi(narve.get("roi_pct")), _roi(fm.get("roi_pct")), _roi(fe.get("roi_pct"))),
        ("Win rate", _pct(narve.get("win_rate")), _pct(fm.get("win_rate")), _pct(fe.get("win_rate"))),
        ("Sharpe", _num(narve.get("sharpe")), _num(fm.get("sharpe")), _num(fe.get("sharpe"))),
        ("Max drawdown", _pct(narve.get("max_drawdown")), _pct(fm.get("max_drawdown")), _pct(fe.get("max_drawdown"))),
        ("Bets placed", str(narve.get("bet_count", 0)), str(fm.get("bet_count", 0)), str(fe.get("bet_count", 0))),
        ("Final bankroll", _money(narve.get("final_bankroll")), _money(fm.get("final_bankroll")), _money(fe.get("final_bankroll"))),
    ]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * 4) + " |"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _md_bet_table(bets: list[dict]) -> str:
    header = (
        "| # | Date | Market | narve P | Mkt price | Edge | Side | Stake | Outcome | P&L | Bankroll |"
    )
    sep = "| " + " | ".join(["---"] * 11) + " |"
    lines = [header, sep]
    for i, b in enumerate(bets, 1):
        q = (b.get("question") or b.get("market_id") or "")
        q = q[:48] + ("…" if len(q) > 48 else "")
        lines.append(
            "| "
            + " | ".join([
                str(i),
                str(b.get("date", "")),
                q.replace("|", "/"),
                _pct(b.get("narve_probability")),
                _pct(b.get("market_price")),
                _signed_pct(b.get("edge")),
                str(b.get("direction", "")),
                _money(b.get("stake")),
                str(b.get("outcome", "")),
                _money(b.get("pnl")),
                _money(b.get("bankroll_after")),
            ])
            + " |"
        )
    if not bets:
        lines.append("| — | — | _no bets placed_ | — | — | — | — | — | — | — | — |")
    return "\n".join(lines)


def _md_contributions(bets: list[dict]) -> str:
    """Per-bet as-of credibility breakdown — the auditable 'why' for each bet."""
    blocks: list[str] = []
    for i, b in enumerate(bets, 1):
        contribs = b.get("contributions") or []
        if not contribs:
            continue
        head = f"**Bet {i} — {b.get('date','')} — {b.get('market_id','')}**"
        sub = ["| Source | as-of credibility | stated P(YES) |",
               "| --- | --- | --- |"]
        for c in contribs:
            if not isinstance(c, dict):
                continue
            sub.append(
                f"| {c.get('source_handle','?')} | {_num(c.get('credibility'), 4)} | {_pct(c.get('forecast_probability'))} |"
            )
        sub.append(
            f"| **narve weighted P(YES)** | | **{_pct(b.get('narve_probability'))}** "
            f"(market {_pct(b.get('market_price'))}, edge {_signed_pct(b.get('edge'))}) |"
        )
        blocks.append(head + "\n\n" + "\n".join(sub))
    return "\n\n".join(blocks) if blocks else "_No per-source credibility contributions were logged._"


def _render_markdown(
    methods: dict,
    *,
    dataset_label: Optional[str],
    generated_at: str,
) -> str:
    parts: list[str] = []
    parts.append("# narve walk-forward backtest — proof report")
    parts.append("")
    parts.append(
        "**Thesis under test:** credibility-scored social-media forecasters beat "
        "the prediction market, with **zero lookahead**."
    )
    parts.append("")
    parts.append(f"- **Generated:** {generated_at}")
    if dataset_label:
        parts.append(f"- **Dataset:** {dataset_label}")
    parts.append(
        "- **Methodologies:** " + ", ".join(methods.keys())
        + " (both cold-start methods reported side by side)"
    )
    parts.append(
        "- **Baselines:** always-follow-market, flat/unweighted ensemble "
        "(scored by the same engine on the same markets/dates/stakes)"
    )
    parts.append("")

    # N honesty banner — distinct markets across all methods.
    market_ids: set[str] = set()
    total_bets = 0
    for out in methods.values():
        for d in out.get("decisions", []):
            market_ids.add(str(d.get("market_id", "")))
            total_bets += 1
    parts.append(
        f"> **Sample size (N):** {len(market_ids)} distinct markets; "
        f"{total_bets} total bets logged across methodologies. "
        "Small-N demo — every bet is listed below and is individually auditable. "
        "This number does not imply more data than exists."
    )
    parts.append("")

    # Per-method sections.
    for method_label, out in methods.items():
        decisions = out.get("decisions", [])
        results = dict(out.get("results") or {})
        starting = _starting_bankroll_of(out)
        results.setdefault("starting_bankroll", starting)
        narve_bets = _narve_bets_from_decisions(decisions, starting)
        baselines = score_baselines(decisions, starting_bankroll=starting)

        parts.append(f"## Cold-start methodology: {method_label}")
        parts.append("")
        parts.append(f"N = **{len(decisions)} bets** on **{len({d.get('market_id') for d in decisions})} markets**. "
                     f"Starting bankroll {_money(starting)}.")
        parts.append("")
        parts.append("### narve vs. baselines")
        parts.append("")
        parts.append(_md_metrics_table(results, baselines))
        parts.append("")
        parts.append(
            "_Follow-market believes the price exactly, so it has no edge and "
            "never bets (0% by construction — the null hypothesis). The flat "
            "ensemble bets the same markets/stakes as narve but equal-weights "
            "every forecaster, ignoring credibility._"
        )
        parts.append("")
        parts.append("### Per-bet detail (auditable, bet-by-bet)")
        parts.append("")
        parts.append(_md_bet_table(narve_bets))
        parts.append("")
        parts.append("### As-of credibility inputs per bet")
        parts.append("")
        parts.append(_md_contributions(narve_bets))
        parts.append("")

    parts.append("---")
    parts.append("")
    parts.append(
        "_Integrity: no-lookahead is enforced at dataset load "
        "(`gateway/backtest_dataset.py`) and again per-decision in the replay "
        "harness. Baselines are scored by the same engine code path as narve "
        "(`gateway/backtest.py`). This is a proof document, not a designed page._"
    )
    parts.append("")
    return "\n".join(parts)


# ── HTML rendering (self-contained, monochrome) ────────────────────────────────


def _h(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _html_metrics_table(narve: dict, baselines: dict) -> str:
    fm = baselines["follow_market"]
    fe = baselines["flat_ensemble"]
    rows = [
        ("ROI", _roi(narve.get("roi_pct")), _roi(fm.get("roi_pct")), _roi(fe.get("roi_pct"))),
        ("Win rate", _pct(narve.get("win_rate")), _pct(fm.get("win_rate")), _pct(fe.get("win_rate"))),
        ("Sharpe", _num(narve.get("sharpe")), _num(fm.get("sharpe")), _num(fe.get("sharpe"))),
        ("Max drawdown", _pct(narve.get("max_drawdown")), _pct(fm.get("max_drawdown")), _pct(fe.get("max_drawdown"))),
        ("Bets placed", str(narve.get("bet_count", 0)), str(fm.get("bet_count", 0)), str(fe.get("bet_count", 0))),
        ("Final bankroll", _money(narve.get("final_bankroll")), _money(fm.get("final_bankroll")), _money(fe.get("final_bankroll"))),
    ]
    body = "".join(
        "<tr><th scope=\"row\">" + _h(r[0]) + "</th>"
        + "".join(f"<td>{_h(c)}</td>" for c in r[1:]) + "</tr>"
        for r in rows
    )
    return (
        "<table class=\"metrics\"><thead><tr>"
        "<th>Metric</th><th>narve (credibility-weighted)</th>"
        "<th>Follow market</th><th>Flat ensemble</th>"
        "</tr></thead><tbody>" + body + "</tbody></table>"
    )


def _html_bet_table(bets: list[dict]) -> str:
    head = (
        "<tr><th>#</th><th>Date</th><th>Market</th><th>narve P</th>"
        "<th>Mkt price</th><th>Edge</th><th>Side</th><th>Stake</th>"
        "<th>Outcome</th><th>P&amp;L</th><th>Bankroll</th></tr>"
    )
    if not bets:
        body = "<tr><td colspan=\"11\" class=\"empty\">no bets placed</td></tr>"
    else:
        cells = []
        for i, b in enumerate(bets, 1):
            q = b.get("question") or b.get("market_id") or ""
            outcome = str(b.get("outcome", ""))
            cells.append(
                "<tr>"
                f"<td>{i}</td>"
                f"<td>{_h(b.get('date',''))}</td>"
                f"<td title=\"{_h(b.get('market_id',''))}\">{_h(q)}</td>"
                f"<td>{_h(_pct(b.get('narve_probability')))}</td>"
                f"<td>{_h(_pct(b.get('market_price')))}</td>"
                f"<td>{_h(_signed_pct(b.get('edge')))}</td>"
                f"<td>{_h(b.get('direction',''))}</td>"
                f"<td>{_h(_money(b.get('stake')))}</td>"
                f"<td class=\"o-{_h(outcome)}\">{_h(outcome)}</td>"
                f"<td>{_h(_money(b.get('pnl')))}</td>"
                f"<td>{_h(_money(b.get('bankroll_after')))}</td>"
                "</tr>"
            )
        body = "".join(cells)
    return f"<table class=\"bets\"><thead>{head}</thead><tbody>{body}</tbody></table>"


def _html_contributions(bets: list[dict]) -> str:
    blocks: list[str] = []
    for i, b in enumerate(bets, 1):
        contribs = b.get("contributions") or []
        if not contribs:
            continue
        rows = []
        for c in contribs:
            if not isinstance(c, dict):
                continue
            rows.append(
                f"<tr><td>{_h(c.get('source_handle','?'))}</td>"
                f"<td>{_h(_num(c.get('credibility'),4))}</td>"
                f"<td>{_h(_pct(c.get('forecast_probability')))}</td></tr>"
            )
        rows.append(
            "<tr class=\"sumrow\"><td><strong>narve weighted P(YES)</strong></td><td></td>"
            f"<td><strong>{_h(_pct(b.get('narve_probability')))}</strong> "
            f"(mkt {_h(_pct(b.get('market_price')))}, edge {_h(_signed_pct(b.get('edge')))})</td></tr>"
        )
        blocks.append(
            f"<details><summary>Bet {i} — {_h(b.get('date',''))} — {_h(b.get('market_id',''))}</summary>"
            "<table class=\"contrib\"><thead><tr><th>Source</th>"
            "<th>as-of credibility</th><th>stated P(YES)</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></details>"
        )
    if not blocks:
        return "<p class=\"muted\">No per-source credibility contributions were logged.</p>"
    return "".join(blocks)


_HTML_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       color: #111; background: #fff; margin: 0; padding: 2rem 1.25rem; line-height: 1.45; }
.wrap { max-width: 1040px; margin: 0 auto; }
h1 { font-size: 1.45rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin: 2.25rem 0 .5rem; border-top: 1px solid #000; padding-top: 1rem; }
h3 { font-size: .95rem; margin: 1.5rem 0 .4rem; text-transform: uppercase; letter-spacing: .04em; }
p { margin: .4rem 0; }
.muted, .meta { color: #555; font-size: .82rem; }
.meta { margin-bottom: .75rem; }
.meta li { margin: .1rem 0; }
.banner { border: 1px solid #000; padding: .65rem .8rem; margin: 1rem 0; background: #f5f5f5;
          font-size: .85rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; font-size: .8rem; }
th, td { border: 1px solid #ccc; padding: .3rem .5rem; text-align: right; }
th { background: #f0f0f0; text-align: right; }
table.metrics th[scope=row], table.metrics thead th:first-child { text-align: left; }
table.metrics td:first-child, table.bets td:nth-child(3), table.contrib td:first-child,
table.contrib th:first-child { text-align: left; }
table.bets td:nth-child(2), table.bets td:nth-child(7) { text-align: center; }
.o-win { color: #060; } .o-loss { color: #900; }
.sumrow td { border-top: 2px solid #000; background: #fafafa; }
details { border: 1px solid #ddd; margin: .35rem 0; padding: .25rem .5rem; }
summary { cursor: pointer; font-size: .82rem; }
.note { font-size: .78rem; color: #444; font-style: italic; }
footer { margin-top: 2.5rem; border-top: 1px solid #000; padding-top: .75rem;
         font-size: .78rem; color: #444; }
""".strip()


def _render_html(
    methods: dict,
    *,
    dataset_label: Optional[str],
    generated_at: str,
) -> str:
    market_ids: set[str] = set()
    total_bets = 0
    for out in methods.values():
        for d in out.get("decisions", []):
            market_ids.add(str(d.get("market_id", "")))
            total_bets += 1

    sections: list[str] = []
    for method_label, out in methods.items():
        decisions = out.get("decisions", [])
        results = dict(out.get("results") or {})
        starting = _starting_bankroll_of(out)
        results.setdefault("starting_bankroll", starting)
        narve_bets = _narve_bets_from_decisions(decisions, starting)
        baselines = score_baselines(decisions, starting_bankroll=starting)
        n_markets = len({d.get("market_id") for d in decisions})

        sections.append(
            f"<h2>Cold-start methodology: {_h(method_label)}</h2>"
            f"<p class=\"meta\">N = <strong>{len(decisions)} bets</strong> on "
            f"<strong>{n_markets} markets</strong>. Starting bankroll "
            f"{_h(_money(starting))}.</p>"
            "<h3>narve vs. baselines</h3>"
            + _html_metrics_table(results, baselines)
            + "<p class=\"note\">Follow-market believes the price exactly, so it "
              "has no edge and never bets (0% by construction — the null "
              "hypothesis). The flat ensemble bets the same markets/stakes as "
              "narve but equal-weights every forecaster, ignoring credibility.</p>"
            "<h3>Per-bet detail (auditable, bet-by-bet)</h3>"
            + _html_bet_table(narve_bets)
            + "<h3>As-of credibility inputs per bet</h3>"
            + _html_contributions(narve_bets)
        )

    meta = ["<li><strong>Generated:</strong> " + _h(generated_at) + "</li>"]
    if dataset_label:
        meta.append("<li><strong>Dataset:</strong> " + _h(dataset_label) + "</li>")
    meta.append(
        "<li><strong>Methodologies:</strong> " + _h(", ".join(methods.keys()))
        + " (both cold-start methods, side by side)</li>"
    )
    meta.append(
        "<li><strong>Baselines:</strong> always-follow-market, flat/unweighted "
        "ensemble (same engine, same markets/dates/stakes)</li>"
    )

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>narve walk-forward backtest — proof report</title>"
        f"<style>{_HTML_STYLE}</style></head><body><div class=\"wrap\">"
        "<h1>narve walk-forward backtest — proof report</h1>"
        "<p>Thesis under test: <strong>credibility-scored social-media "
        "forecasters beat the prediction market</strong>, with zero lookahead.</p>"
        "<ul class=\"meta\">" + "".join(meta) + "</ul>"
        "<div class=\"banner\"><strong>Sample size (N):</strong> "
        f"{len(market_ids)} distinct markets; {total_bets} total bets logged "
        "across methodologies. Small-N demo — every bet is listed and "
        "individually auditable. This number does not imply more data than "
        "exists.</div>"
        + "".join(sections)
        + "<footer>Integrity: no-lookahead is enforced at dataset load "
          "(<code>gateway/backtest_dataset.py</code>) and again per-decision in "
          "the replay harness. Baselines are scored by the same engine code path "
          "as narve (<code>gateway/backtest.py</code>). Proof document, not a "
          "designed page.</footer>"
        "</div></body></html>"
    )


# ── public entrypoint ──────────────────────────────────────────────────────────


def build_report(
    replay_outputs_by_method: dict,
    *,
    out_dir: Optional[Union[str, Path]] = None,
    dataset_label: Optional[str] = None,
) -> dict:
    """Render the proof report (markdown + self-contained HTML) and write both.

    Args:
        replay_outputs_by_method: ``{method_label: ReplayOutput}`` where each
            ``ReplayOutput`` is the harness dict ``{"decisions": [...],
            "results": {...}}``. Order is preserved in the report (use an
            ordered mapping to control left-to-right / top-to-bottom order).
        out_dir: directory to write ``report.md`` and ``report.html`` into.
            Defaults to ``gateway/data/backtest/``.
        dataset_label: optional human label for the dataset used (e.g. the
            file path or "synthetic fixture"), surfaced in the report header.

    Returns:
        ``{"markdown": <Path to report.md>, "html": <Path to report.html>}``.

    Raises:
        ValueError: if ``replay_outputs_by_method`` is empty or an entry is not
            a dict with a ``decisions`` list and ``results`` dict.
    """
    if not replay_outputs_by_method:
        raise ValueError("replay_outputs_by_method is empty — nothing to report")

    methods: dict = {}
    for label, out in replay_outputs_by_method.items():
        if not isinstance(out, dict) or "decisions" not in out or "results" not in out:
            raise ValueError(
                f"methodology {label!r}: expected a harness output dict with "
                "'decisions' and 'results' keys"
            )
        if not isinstance(out["decisions"], list):
            raise ValueError(f"methodology {label!r}: 'decisions' must be a list")
        if not isinstance(out["results"], dict):
            raise ValueError(f"methodology {label!r}: 'results' must be a dict")
        methods[label] = out

    out_path = Path(out_dir) if out_dir is not None else DEFAULT_OUT_DIR
    out_path.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    md = _render_markdown(methods, dataset_label=dataset_label, generated_at=generated_at)
    html_doc = _render_html(methods, dataset_label=dataset_label, generated_at=generated_at)

    md_path = out_path / "report.md"
    html_path = out_path / "report.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(html_doc, encoding="utf-8")

    return {"markdown": md_path, "html": html_path}


# ── inline stub sample + smoke test ────────────────────────────────────────────


def _stub_replay_output(method_label: str, *, narve_edge: bool) -> dict:
    """A tiny, hand-built ReplayOutput matching the documented consumed shape.

    Used to exercise rendering without the real harness. ``narve_edge=True``
    makes narve right; the flat ensemble is deliberately closer to the market
    so narve's credibility weighting shows separation. Numbers here are
    illustrative, not a real backtest.
    """
    starting = _DEFAULT_BANKROLL
    # Two markets: narve takes the correct side on both via credibility weighting.
    decisions = [
        {
            "date": "2026-09-15",
            "market_id": "synthetic-senate-control-2026",
            "question": "[SYNTHETIC] Will Party A hold the Senate majority?",
            "credibility_contributions": {
                "@oracle_always_right": {"credibility": 0.92, "predicted_probability": 0.96},
                "@clueless_always_wrong": {"credibility": 0.08, "predicted_probability": 0.05},
                "@noisy_coinflip": {"credibility": 0.50, "predicted_probability": 0.52},
            },
            "narve_probability": 0.88 if narve_edge else 0.60,
            "market_price": 0.55,
            "edge": (0.88 if narve_edge else 0.60) - 0.55,
            "direction": "YES",
            "stake": 1500.0,
            "resolved_correct": True,
            "pnl": 1500.0 * (1 - 0.55) / 0.55,
        },
        {
            "date": "2026-10-10",
            "market_id": "synthetic-ballot-measure-7-2026",
            "question": "[SYNTHETIC] Will Ballot Measure 7 pass?",
            "credibility_contributions": {
                "@oracle_always_right": {"credibility": 0.93, "predicted_probability": 0.05},
                "@clueless_always_wrong": {"credibility": 0.07, "predicted_probability": 0.94},
                "@noisy_coinflip": {"credibility": 0.50, "predicted_probability": 0.50},
            },
            "narve_probability": 0.12,
            "market_price": 0.39,
            "edge": 0.12 - 0.39,  # narve believes NO; bets the NO side
            "direction": "NO",
            "stake": 1200.0,
            "resolved_correct": True,
            "pnl": 1200.0 * (1 - (1 - 0.39)) / (1 - 0.39),
        },
    ]
    # Score narve's headline results from the decisions via the engine mechanics.
    rows = [
        {
            "date": d["date"], "market_id": d["market_id"], "question": d["question"],
            "price": d["market_price"], "p_belief": d["narve_probability"],
            "direction": d["direction"], "stake": d["stake"],
            "outcome": _decision_outcome(d),
        }
        for d in decisions
    ]
    narve_results = _replay_from_rows(rows, starting)
    return {
        "decisions": decisions,
        "results": {
            "roi_pct": narve_results["roi_pct"],
            "win_rate": narve_results["win_rate"],
            "sharpe": narve_results["sharpe"],
            "max_drawdown": narve_results["max_drawdown"],
            "bet_count": narve_results["bet_count"],
            "final_bankroll": narve_results["final_bankroll"],
            "starting_bankroll": starting,
        },
    }


if __name__ == "__main__":  # pragma: no cover — manual smoke test
    import sys
    import tempfile

    sample = {
        "Strict two-window": _stub_replay_output("Strict two-window", narve_edge=True),
        "Bayesian prior": _stub_replay_output("Bayesian prior", narve_edge=True),
    }
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
    paths = build_report(sample, out_dir=target, dataset_label="inline stub sample (NOT real data)")
    md = paths["markdown"].read_text(encoding="utf-8")
    html_doc = paths["html"].read_text(encoding="utf-8")
    print(f"wrote: {paths['markdown']}  ({len(md)} chars)")
    print(f"wrote: {paths['html']}  ({len(html_doc)} chars)")
    # Light assertions so the smoke test fails loudly if rendering breaks.
    assert "narve vs. baselines" in md, "metrics section missing in markdown"
    assert "Follow market" in md and "Flat ensemble" in md, "baselines missing"
    assert "Strict two-window" in md and "Bayesian prior" in md, "both methods missing"
    assert "Per-bet detail" in md, "per-bet table missing"
    assert "Sample size (N)" in md, "N statement missing"
    assert html_doc.startswith("<!doctype html>"), "html not self-contained"
    assert "<style>" in html_doc, "html style missing"
    print("smoke assertions passed")
