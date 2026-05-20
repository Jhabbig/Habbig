"""Quarter-Kelly position-size suggestions for edges.

Strictly informational. Given a market price, the mispricing score, and the
spread, returns a suggested fraction-of-bankroll size between 0 and a hard
cap (default 5%). Reads as "if this were a real trade, here's how much
size the math says you'd take".

The numbers are not advice. The calc:

  edge      = clip(mispricing_score * 0.02, 0, 0.10)   # implied prob edge
  kelly     = edge / (price / (1 - price))             # full Kelly fraction
  fractional = kelly * 0.25                            # quarter-Kelly
  haircut   = max(0.5, 1 - spread_bps / 1000)          # wide-spread discount
  suggested = min(max_pct, fractional * haircut)

Quarter-Kelly because full Kelly is famously too aggressive in the real
world; the spread haircut shrinks the size for thinly-traded contracts.
"""

from __future__ import annotations

import os


def _max_pct() -> float:
    try:
        return float(os.environ.get("CULTURE_POSITION_MAX_PCT", "0.05"))
    except ValueError:
        return 0.05


def _kelly_fraction() -> float:
    try:
        return float(os.environ.get("CULTURE_POSITION_KELLY_FRACTION", "0.25"))
    except ValueError:
        return 0.25


def _edge_per_z() -> float:
    """Implied probability edge per unit of mispricing score."""
    try:
        return float(os.environ.get("CULTURE_POSITION_EDGE_PER_Z", "0.02"))
    except ValueError:
        return 0.02


def suggest(price: float | None,
            mispricing_score: float | None,
            spread_bps: float | None) -> dict | None:
    """Return a sizing suggestion dict, or None if inputs are unusable."""
    if price is None or mispricing_score is None:
        return None
    if not (0 < price < 1):
        return None
    if mispricing_score <= 0:
        return None

    edge = min(0.10, max(0.0, mispricing_score * _edge_per_z()))
    if edge <= 0:
        return None

    odds = price / (1 - price)
    if odds <= 0:
        return None
    kelly_full = edge / odds
    fractional = kelly_full * _kelly_fraction()
    haircut = max(0.5, 1 - (spread_bps or 0) / 1000)
    suggested = min(_max_pct(), fractional * haircut)
    if suggested <= 0:
        return None

    return {
        "size_pct": round(suggested, 4),
        "edge_pct": round(edge, 4),
        "kelly_full_pct": round(kelly_full, 4),
        "kelly_fraction": _kelly_fraction(),
        "haircut": round(haircut, 3),
        "tier": _tier(suggested),
    }


def _tier(size_pct: float) -> str:
    if size_pct < 0.005:
        return "shrug"
    if size_pct < 0.015:
        return "watch"
    if size_pct < 0.03:
        return "tracker"
    return "conviction"
