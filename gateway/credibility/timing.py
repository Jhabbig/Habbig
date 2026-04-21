"""Market-timing score for individual predictions.

Rewards sources who predict *early* and *contrarian*, not just correctly.
Score is on [0, 1]:

  time_component       = min(days_remaining / 30, 1.0)
  contrarian_component = YES at market < 0.40  → (0.40 − market) * 2
                         NO  at market > 0.60  → (market − 0.60) * 2
                         otherwise              0
  outcome_component    = 1.5 if correct else 0.0

  score = clamp((time + contrarian) * outcome / 2, 0, 1)

The *2 in contrarian rescales a maximum market distance of 0.5 to a
bounded [0, 1] component. The 1.5 outcome multiplier makes a correct
bet at 35¢ (big contrarian edge) score well above a correct bet at 70¢
(already obvious).

Pure function — unit-testable in isolation. Called on resolution for
every record. ``compute_timing_score`` also returns the raw
``edge_at_prediction`` so the pipeline can store it for the source
detail "Timing profile" section.
"""

from __future__ import annotations

from typing import Any


def compute_timing_score(
    predicted_at: Any,
    market_close_time: Any,
    market_implied_at_prediction: float,
    resolved_correct: Any,
    predicted_direction: str,
) -> dict:
    """Return ``{timing_score, edge_at_prediction, components}``.

    ``predicted_at`` + ``market_close_time`` can be datetime objects, ISO
    strings, or unix-seconds ints — we normalise. If either is missing
    ``time_component`` falls back to 0.5 (neutral) rather than 0, so a
    correct call still gets credit even when timestamps are incomplete.

    ``predicted_direction`` is "YES" / "yes" / "NO" / "no". Anything else
    yields a zero contrarian bonus (we can't tell which side they took).

    ``resolved_correct`` can be 0/1, True/False, or None; None → 0.
    """
    days_remaining = _days_remaining(predicted_at, market_close_time)
    time_component = min(days_remaining / 30.0, 1.0) if days_remaining is not None else 0.5
    time_component = max(0.0, time_component)

    # Edge at prediction — how far from 0.5 was the market?
    try:
        market = float(market_implied_at_prediction)
    except (TypeError, ValueError):
        market = 0.5
    market = max(0.0, min(1.0, market))

    direction = str(predicted_direction or "").strip().upper()
    if direction == "YES" and market < 0.40:
        contrarian_component = (0.40 - market) * 2.0  # up to 0.8
    elif direction == "NO" and market > 0.60:
        contrarian_component = (market - 0.60) * 2.0
    else:
        contrarian_component = 0.0

    outcome_component = 1.5 if _bool(resolved_correct) else 0.0

    raw = (time_component + contrarian_component) * outcome_component / 2.0
    score = max(0.0, min(1.0, raw))

    # Edge is absolute distance from market's implied probability for the
    # side the source took. Correct side → edge = how much they disagreed
    # with the market in their direction. Used for "avg edge captured".
    if direction == "YES":
        edge = max(0.0, 0.5 - market) if market < 0.5 else 0.0
    elif direction == "NO":
        edge = max(0.0, market - 0.5) if market > 0.5 else 0.0
    else:
        edge = 0.0

    return {
        "timing_score": round(score, 6),
        "edge_at_prediction": round(edge, 6),
        "components": {
            "time": round(time_component, 6),
            "contrarian": round(contrarian_component, 6),
            "outcome": outcome_component,
        },
    }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _bool(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return False


def _to_unix(ts: Any) -> float | None:
    import datetime as _dt
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, _dt.datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return ts.timestamp()
    if isinstance(ts, str):
        try:
            parsed = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None
    return None


def _days_remaining(predicted_at: Any, market_close_time: Any) -> float | None:
    a = _to_unix(predicted_at)
    b = _to_unix(market_close_time)
    if a is None or b is None:
        return None
    return max(0.0, (b - a) / 86400.0)
