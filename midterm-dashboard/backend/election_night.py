"""Election-night mode: synthetic "narve.ai call" state machine + chamber-level
aggregation for live race-night viewing.

Two responsibilities:

  1. Classify each race into a **call state** — Called D, Called R, Lean D,
     Lean R, or Tossup — based on the house forecast, smart-money agreement,
     and source spread. We label the calls as "narve.ai call" so it's clear
     they aren't AP / DDHQ decision-desk calls.

  2. Aggregate per-chamber totals (Senate D / R / uncalled; House D / R /
     uncalled) and compute the polling-vs-market gap so users can see how
     much the markets diverged from late-cycle polling on the night.

This module is read-only — it doesn't touch the DB. The caller passes in
forecasts and a polling-average lookup; we return a structured payload the
frontend renders as the election-night view.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Thresholds that drive the call state machine. Numbers are deliberately
# strict because mis-calling a race on race night is the worst failure mode.
# A call also requires high ensemble confidence (≥ CONFIDENCE_TO_CALL) so
# we don't call races on a single thin-data source.
CALL_THRESHOLD_D = 0.90        # forecast_d ≥ this → called D (subject to confidence)
CALL_THRESHOLD_R = 0.10        # forecast_d ≤ this → called R
LEAN_THRESHOLD_D = 0.65
LEAN_THRESHOLD_R = 0.35
CONFIDENCE_TO_CALL = 0.55      # required ensemble confidence for any "Called" state

CALL_CALLED_D = "called_d"
CALL_CALLED_R = "called_r"
CALL_LEAN_D = "lean_d"
CALL_LEAN_R = "lean_r"
CALL_TOSSUP = "tossup"

CALL_LABELS: dict[str, str] = {
    CALL_CALLED_D: "Called D",
    CALL_CALLED_R: "Called R",
    CALL_LEAN_D: "Lean D",
    CALL_LEAN_R: "Lean R",
    CALL_TOSSUP: "Tossup",
}


def classify_call(
    *,
    forecast_d: Optional[float],
    confidence: float,
    smart_money_direction: Optional[str] = None,
) -> str:
    """Return one of the CALL_* constants for a race.

    A "called" state requires both an extreme forecast AND high ensemble
    confidence. When a smart-money direction is supplied we ALSO require it
    to agree — a market-money divergence near the threshold downgrades the
    call to a lean. That's the conservative election-night posture.
    """
    if forecast_d is None:
        return CALL_TOSSUP

    p = float(forecast_d)
    c = float(confidence or 0.0)

    if p >= CALL_THRESHOLD_D and c >= CONFIDENCE_TO_CALL:
        # Smart money must agree (or be absent) to upgrade lean → call.
        if smart_money_direction is None or smart_money_direction == "D":
            return CALL_CALLED_D
        return CALL_LEAN_D
    if p <= CALL_THRESHOLD_R and c >= CONFIDENCE_TO_CALL:
        if smart_money_direction is None or smart_money_direction == "R":
            return CALL_CALLED_R
        return CALL_LEAN_R
    if p >= LEAN_THRESHOLD_D:
        return CALL_LEAN_D
    if p <= LEAN_THRESHOLD_R:
        return CALL_LEAN_R
    return CALL_TOSSUP


def polling_gap(
    *,
    forecast_d: Optional[float],
    polling_avg: Optional[float],
) -> Optional[float]:
    """Return the signed market-minus-polling gap, in percentage points.

    Positive = market is more bullish on D than polling; negative = market is
    more bullish on R than polling. ``None`` if either side is missing.
    """
    if forecast_d is None or polling_avg is None:
        return None
    try:
        return round((float(forecast_d) - float(polling_avg)) * 100, 2)
    except (TypeError, ValueError):
        return None


def aggregate_chamber(
    races: Iterable[dict],
    *,
    chamber: str,
) -> dict:
    """Sum called / lean / tossup counts for one chamber.

    ``chamber`` is matched against the race ``race_type`` field. Known values
    are ``"senate"``, ``"house"``, ``"governor"``. The summary returned here
    powers the big control bars at the top of the election-night view.
    """
    buckets = {
        CALL_CALLED_D: 0,
        CALL_CALLED_R: 0,
        CALL_LEAN_D: 0,
        CALL_LEAN_R: 0,
        CALL_TOSSUP: 0,
    }
    total = 0
    for r in races:
        if (r.get("race_type") or "").lower() != chamber.lower():
            continue
        total += 1
        state = r.get("call_state") or CALL_TOSSUP
        if state in buckets:
            buckets[state] += 1

    called_d = buckets[CALL_CALLED_D]
    called_r = buckets[CALL_CALLED_R]
    lean_d = buckets[CALL_LEAN_D]
    lean_r = buckets[CALL_LEAN_R]
    tossup = buckets[CALL_TOSSUP]

    return {
        "chamber": chamber,
        "total": total,
        "called_d": called_d,
        "called_r": called_r,
        "lean_d": lean_d,
        "lean_r": lean_r,
        "tossup": tossup,
        # Optimistic + pessimistic D outcomes given current leans
        "d_floor": called_d,
        "d_ceiling": called_d + lean_d + tossup,
        "r_floor": called_r,
        "r_ceiling": called_r + lean_r + tossup,
    }


def assemble_election_night(
    *,
    forecasts: list[dict],
    polling_by_race: Optional[dict[str, float]] = None,
) -> dict:
    """Combine forecasts + polling into the election-night payload.

    Args:
      forecasts: Rows from ``/data/forecasts`` (with ``smart_money`` inlined).
      polling_by_race: Optional ``{race_key: polling_avg_d}`` map.

    Returns::

        {
          "races": [...],                     # enriched with call_state + polling_gap
          "chambers": {                       # senate / house / governor summaries
            "senate":   {...},
            "house":    {...},
            "governor": {...},
          },
          "counts": {                         # top-level numbers for the hero strip
            "total_races": int,
            "called": int,
            "leans": int,
            "tossups": int,
          },
        }
    """
    polling_by_race = polling_by_race or {}

    races: list[dict] = []
    counts = {"called": 0, "leans": 0, "tossups": 0}
    for f in forecasts:
        rk = f.get("race_key") or ""
        sm = f.get("smart_money") or {}
        smart_dir = sm.get("direction") if sm.get("available") else None
        call_state = classify_call(
            forecast_d=f.get("forecast_d"),
            confidence=f.get("confidence", 0),
            smart_money_direction=smart_dir,
        )
        polling = polling_by_race.get(rk)
        gap = polling_gap(forecast_d=f.get("forecast_d"), polling_avg=polling)

        if call_state in (CALL_CALLED_D, CALL_CALLED_R):
            counts["called"] += 1
        elif call_state in (CALL_LEAN_D, CALL_LEAN_R):
            counts["leans"] += 1
        else:
            counts["tossups"] += 1

        races.append({
            **f,
            "call_state": call_state,
            "call_label": CALL_LABELS[call_state],
            "polling_avg_d": polling,
            "polling_gap_pp": gap,
        })

    chambers = {
        "senate":   aggregate_chamber(races, chamber="senate"),
        "house":    aggregate_chamber(races, chamber="house"),
        "governor": aggregate_chamber(races, chamber="governor"),
    }

    return {
        "races": races,
        "chambers": chambers,
        "counts": {
            **counts,
            "total_races": len(races),
        },
    }
