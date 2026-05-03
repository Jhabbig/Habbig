"""Right-Now synthesis layer — one paragraph that summarizes the whole dashboard.

The dashboard has many panels. A first-time user (or an expert in a hurry)
wants a single line that answers: *what's actually happening, and what
should I look at?* This module pulls the cached output of every analysis
already in the dashboard and synthesizes:

  * **Headline**     — one declarative sentence about the next FOMC and how
                       the market is pricing it.
  * **Subhead**      — 1-2 sentences listing notable recent moves
                       (macro surprises, stance drifts, top arb opportunity).
  * **Signals list** — structured bullets the frontend can render as
                       click-to-scroll chips, each with a tone tag
                       (``hawkish`` / ``dovish`` / ``actionable`` / ``neutral``)
                       and a target ``panel_id`` for scrolling.
  * **Warnings**     — data freshness / availability flags so users know if
                       a panel below is degraded.

This module **does not fetch any new data** — it composes from the existing
caches (``implied_path``, ``ois_curve``, ``edge``, ``stance``,
``econ_releases``). So it's cheap to call and re-call.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock

from analysis import edge as edge_analysis
from analysis import stance as stance_analysis
from ingestion import decision_calendar, econ_releases, fred_client, implied_path

log = logging.getLogger(__name__)

# Synthesis is cheap *if* the downstream caches are warm. Cold path can be
# 30-60s if every dependency has to fetch. Cache the synthesized output for
# 60s so the dashboard's frontend gets a snappy response even when several
# users hit the page simultaneously.
_CACHE: dict = {"data": None, "fetched_at": 0.0}
_CACHE_TTL = 60
_lock = Lock()

# Threshold below which we don't surface a signal — keeps noise out of the
# headline. Same 3-pp threshold as the edge table, plus per-signal heuristics.
_ARB_THRESHOLD_PP = 3.0
_MACRO_SURPRISE_PP = 0.1   # MoM/YoY change vs prior >0.1pp is "notable"
_STANCE_DRIFT_THRESHOLD = 0.25


@dataclass
class Signal:
    kind: str
    text: str
    tone: str            # "hawkish" | "dovish" | "actionable" | "neutral" | "data"
    panel_id: str | None = None  # CSS id of the panel to scroll to on click

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "tone": self.tone, "panel_id": self.panel_id}


@dataclass
class Brief:
    as_of: str
    next_meeting: dict | None
    headline: str
    subhead: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "next_meeting": self.next_meeting,
            "headline": self.headline,
            "subhead": self.subhead,
            "signals": [s.to_dict() for s in self.signals],
            "warnings": self.warnings,
        }


# --- Headline composition --------------------------------------------------

def _market_consensus_phrase(edge_data: dict) -> str | None:
    """Return a phrase like '95% HOLD priced' or '60% CUT25 priced' from the
    most-liquid bucket on either venue, or None if no live markets."""
    if not edge_data or not edge_data.get("rows"):
        return None
    # Pick the row with the highest YES price across either venue — that's
    # the outcome the market thinks is most likely.
    candidates = []
    for r in edge_data["rows"]:
        for venue, price_key in [("Poly", "polymarket_price"), ("Kalshi", "kalshi_price")]:
            p = r.get(price_key)
            if p is not None:
                candidates.append((p, r["outcome_bucket"], venue))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    p, bucket, venue = candidates[0]
    if p < 0.40:
        # No outcome dominates — markets are split, not a clean "X is priced".
        return f"no clear consensus (top: {bucket} at {p*100:.0f}% {venue})"
    return f"{p*100:.0f}% {bucket.upper()} ({venue})"


def _when_phrase(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def _build_headline(soonest: dict | None, fomc: dict | None, edge_data: dict, implied: dict) -> str:
    """Lead with the soonest meeting. If FOMC isn't the soonest, append a
    secondary mention so the user always sees its countdown — the dashboard
    is FOMC-centric and most market data only covers that meeting."""
    if not soonest:
        return "No central-bank meeting in the next 90 days."

    soonest_label = soonest["label"]
    soonest_when = _when_phrase(soonest["days_until"])
    is_fomc_soonest = soonest["cb"] == "US"

    # FOMC-specific market data: only meaningful when the headline meeting *is* the FOMC.
    if is_fomc_soonest:
        consensus = _market_consensus_phrase(edge_data)
        if consensus:
            return f"{soonest_label} {soonest_when}. Market is pricing {consensus}."
        if implied and implied.get("delta_bps") is not None:
            delta = implied["delta_bps"]
            verb = "cut" if delta < -1 else "hike" if delta > 1 else "hold"
            if verb == "hold":
                return f"{soonest_label} {soonest_when}. Futures imply no change."
            return f"{soonest_label} {soonest_when}. Futures imply a {abs(delta):.0f} bp {verb}."
        return f"{soonest_label} {soonest_when}. Live market data not yet available."

    # Non-FOMC is sooner — lead with it, append a note about the next FOMC.
    fomc_tail = ""
    if fomc:
        fomc_tail = f" FOMC follows {_when_phrase(fomc['days_until'])}."
    return f"{soonest_label} {soonest_when} — no FOMC market data for this meeting yet.{fomc_tail}"


# --- Subhead composition (recent macro, stance, arb) -----------------------

def _macro_surprise_signals(econ: dict) -> list[Signal]:
    """For each econ series, characterize the latest reading as upside/
    downside surprise vs prior. Returns a list of Signals."""
    out: list[Signal] = []
    for s in econ.get("series", []):
        latest = s.get("latest")
        if not latest:
            continue
        if s.get("is_index"):
            yoy = latest.get("yoy_pct")
            mom = latest.get("mom_pct")
            if yoy is None or mom is None:
                continue
            # Inflation-side: rising prints are hawkish (rate-supportive)
            if mom >= _MACRO_SURPRISE_PP:
                out.append(Signal(
                    kind="macro_surprise",
                    text=f"{s['short_name']} +{mom:.2f}% MoM ({yoy:.1f}% YoY)",
                    tone="hawkish",
                    panel_id="econ-panel",
                ))
            elif mom <= -_MACRO_SURPRISE_PP:
                out.append(Signal(
                    kind="macro_surprise",
                    text=f"{s['short_name']} {mom:.2f}% MoM ({yoy:.1f}% YoY)",
                    tone="dovish",
                    panel_id="econ-panel",
                ))
        else:
            # NFP — labor-side: above-trend payroll growth is hawkish
            ch = latest.get("mom_change_k")
            if ch is None:
                continue
            if ch >= 200:   # >200k is a "strong jobs" print
                out.append(Signal(
                    kind="macro_surprise",
                    text=f"NFP +{ch:.0f}k jobs",
                    tone="hawkish",
                    panel_id="econ-panel",
                ))
            elif ch <= 50:  # weak jobs print
                out.append(Signal(
                    kind="macro_surprise",
                    text=f"NFP only {ch:.0f}k jobs",
                    tone="dovish",
                    panel_id="econ-panel",
                ))
    return out


def _stance_signals(stance: dict) -> list[Signal]:
    """For each CB, surface a stance signal if its norm score crosses the
    drift threshold. Without history we can't compute drift yet, so we just
    report current bucket + score for non-neutral CBs."""
    out: list[Signal] = []
    for r in stance.get("rows", []):
        norm = r.get("norm_score")
        bucket = r.get("bucket")
        if norm is None or bucket == "NEUTRAL":
            continue
        if abs(norm) < _STANCE_DRIFT_THRESHOLD:
            continue
        cb = r.get("cb", "?")
        sign = "+" if norm > 0 else ""
        out.append(Signal(
            kind="stance",
            text=f"{cb} stance {bucket.lower()} (score {sign}{norm:.2f})",
            tone="hawkish" if norm > 0 else "dovish",
            panel_id="stance-panel",
        ))
    return out


def _arb_signal(edge_data: dict) -> Signal | None:
    """Surface the single biggest cross-venue arb opportunity if it crosses
    the threshold. The full ranked table lives in the edge panel."""
    rows = edge_data.get("rows") or []
    threshold = edge_data.get("edge_threshold_pp", _ARB_THRESHOLD_PP)
    arbs = [r for r in rows if r.get("arb_spread_pp") is not None]
    if not arbs:
        return None
    arbs.sort(key=lambda r: abs(r["arb_spread_pp"]), reverse=True)
    top = arbs[0]
    if abs(top["arb_spread_pp"]) < threshold:
        return None
    bucket = top["outcome_bucket"]
    spread = top["arb_spread_pp"]
    side = "Polymarket-rich" if spread > 0 else "Kalshi-rich"
    return Signal(
        kind="arb",
        text=f"{bucket} arb {abs(spread):.1f} pp {side}",
        tone="actionable",
        panel_id="edge-panel",
    )


def _implied_edge_signal(edge_data: dict) -> Signal | None:
    """Surface the largest implied-vs-market edge (model thinks venue is
    mispricing the outcome). Distinct from the cross-venue arb above."""
    rows = edge_data.get("rows") or []
    threshold = edge_data.get("edge_threshold_pp", _ARB_THRESHOLD_PP)
    candidates: list[tuple[float, str, str, str]] = []  # (abs_pp, venue, bucket, signed_pp)
    for r in rows:
        for venue, key in [("Poly", "edge_poly_pp"), ("Kalshi", "edge_kalshi_pp")]:
            v = r.get(key)
            if v is not None and abs(v) >= threshold:
                candidates.append((abs(v), venue, r["outcome_bucket"], v))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    abs_pp, venue, bucket, signed_pp = candidates[0]
    direction = "underprices" if signed_pp > 0 else "overprices"
    return Signal(
        kind="implied_edge",
        text=f"{venue} {direction} {bucket} by {abs_pp:.1f} pp vs futures-implied",
        tone="actionable",
        panel_id="edge-panel",
    )


# --- Warnings (degraded data) ----------------------------------------------

def _collect_warnings(implied: dict, edge_data: dict, stance: dict, econ: dict) -> list[str]:
    out: list[str] = []
    if not implied.get("probabilities"):
        out.append("Implied probabilities unavailable (Yahoo Finance / FRED unreachable).")
    if not edge_data.get("rows"):
        out.append("No live FOMC markets matched on either venue.")
    elif not any(r.get("kalshi_price") is not None for r in edge_data.get("rows", [])):
        out.append("Kalshi FOMC markets currently illiquid — cross-venue arb deferred.")
    if not any(r.get("latest") for r in stance.get("rows", [])):
        out.append("CB statement feeds unreachable.")
    if not any(s.get("latest") for s in econ.get("series", [])):
        out.append("FRED macro data unreachable.")
    return out


# --- Top-level compose ------------------------------------------------------

def compute() -> dict:
    today = datetime.now(timezone.utc)
    cal = decision_calendar.upcoming(today.date(), horizon_days=90)
    next_meeting = cal[0] if cal else None
    fomc = next((m for m in cal if m["cb"] == "US"), None)

    # Pull from existing caches — none of these refetch unless TTL expired.
    implied = implied_path.get_cached() if fomc else {}
    edge_data = edge_analysis.compute() if fomc else {}
    stance = stance_analysis.compute()
    econ = econ_releases.get_cached()

    headline = _build_headline(next_meeting, fomc, edge_data, implied)

    # Build the signals list — the source of truth, frontend renders subset.
    signals: list[Signal] = []
    arb = _arb_signal(edge_data)
    if arb:
        signals.append(arb)
    impl_edge = _implied_edge_signal(edge_data)
    if impl_edge:
        signals.append(impl_edge)
    signals.extend(_macro_surprise_signals(econ))
    signals.extend(_stance_signals(stance))

    # Build subhead from the most important signals — a 1-2 sentence prose
    # version of the chip list. We pick: the top arb (if any), the top macro
    # surprise (if any), and the top stance signal (if any).
    subhead: list[str] = []
    if arb:
        subhead.append(f"Top opportunity: {arb.text}.")
    macro_top = next((s for s in signals if s.kind == "macro_surprise"), None)
    if macro_top:
        tone_word = {"hawkish": "hawkish", "dovish": "dovish"}.get(macro_top.tone, "")
        subhead.append(
            f"Recent macro: {macro_top.text}" + (f" ({tone_word})." if tone_word else ".")
        )
    stance_top = next((s for s in signals if s.kind == "stance"), None)
    if stance_top:
        subhead.append(f"Stance drift: {stance_top.text}.")

    if not subhead:
        subhead.append("No notable signals above threshold right now.")

    warnings = _collect_warnings(implied, edge_data, stance, econ)

    return Brief(
        as_of=today.isoformat(),
        next_meeting=next_meeting,
        headline=headline,
        subhead=subhead,
        signals=signals,
        warnings=warnings,
    ).to_dict()


def get_cached(force: bool = False) -> dict:
    now = time.time()
    with _lock:
        fresh = _CACHE["data"] is not None and (now - _CACHE["fetched_at"]) < _CACHE_TTL
        if fresh and not force:
            return _CACHE["data"]
    data = compute()
    with _lock:
        _CACHE["data"] = data
        _CACHE["fetched_at"] = now
    return data


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(compute(), indent=2))
